"""
Scraper Agent — two-phase data pipeline.

Phase 1 (Nimble SERP API):
  - `google_maps` engine: computer vision extracts structured local-pack results
    including Google Place IDs, addresses, coordinates, and ratings at scale.
  - `google` engine: organic SERP results for review text / snippets.
  Both phases run in parallel.

Phase 2 (Claude):
  - Extracts structured venue signals (noise level, private room, capacity...)
    from the review snippets gathered in Phase 1.

Why Nimble for Place IDs instead of Google directly?
  - Nimble rotates IPs and bypasses anti-bot measures for bulk extraction.
  - No per-request Google billing for the maps search phase.
  - Place IDs extracted this way are legitimate Google identifiers that the
    Google Maps JS API can use to render interactive map markers.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import anthropic
import httpx

from ..tracing import ai_span, http_span
from ..models.models import (
    EnrichedVenue,
    ExtractedSignals,
    NimbleMapsResult,
    RawVenueResult,
    VenueIntent,
)

NIMBLE_API_KEY = os.environ.get("NIMBLE_API_KEY", "")
_NIMBLE_URL = "https://api.webit.live/api/v1/realtime/serp"

_client = anthropic.AsyncAnthropic()
_CLAUDE_SEM = asyncio.Semaphore(5)

# Regex to extract Place ID from Google Maps URLs when Nimble doesn't return it directly
_PLACE_ID_RE = re.compile(r"place_id:([A-Za-z0-9_-]+)")
_CID_RE = re.compile(r"[?&]cid=([0-9]+)")

_SIGNAL_EXTRACTOR_PROMPT = """\
Extract venue attributes from the following review text.
Return ONLY valid JSON — no markdown, no commentary.

Schema (null for unknown fields):
{
  "noise_level": "very_quiet" | "quiet" | "moderate" | "loud" | "very_loud" | null,
  "has_private_room": bool | null,
  "max_group_size": int | null,
  "birthday_friendly": bool | null,
  "wifi_quality": "excellent" | "good" | "poor" | "none" | null,
  "dog_friendly": bool | null,
  "outdoor_seating": bool | null,
  "price_per_head_usd": int | null,
  "booking_difficulty": "easy" | "moderate" | "hard",
  "special_occasion_score": int,    // 0-100
  "birthday_mentions": int,         // count in reviews
  "key_quotes": [string]            // up to 3 relevant short quotes
}

Scoring guidance:
- special_occasion_score 80-100: venue explicitly celebrated for special events
- special_occasion_score 50-79: accommodating but not specialised
- special_occasion_score 0-49: everyday dining, no special-occasion evidence\
"""


async def _call_with_retry(
    raw: RawVenueResult, *, max_attempts: int = 3
) -> ExtractedSignals | None:
    """Extract signals via Claude with exponential-backoff retry."""
    if not raw.snippet:
        return None
    delay = 1.0
    with ai_span(
        "therightspot.extract_signals",
        venue_name=raw.name,
        snippet_length=len(raw.snippet),
    ) as span:
        for attempt in range(max_attempts):
            try:
                async with _CLAUDE_SEM:
                    response = await _client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=512,
                        system=[{
                            "type": "text",
                            "text": _SIGNAL_EXTRACTOR_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }],
                        messages=[{
                            "role": "user",
                            "content": f"Venue: {raw.name}\nText: {raw.snippet}",
                        }],
                    )
                span.set_tag("tokens.input", response.usage.input_tokens)
                span.set_tag("tokens.output", response.usage.output_tokens)
                span.set_tag("attempts", attempt + 1)
                return ExtractedSignals.model_validate(
                    json.loads(response.content[0].text)
                )
            except anthropic.RateLimitError:
                span.set_tag("rate_limited", True)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
            except (json.JSONDecodeError, Exception):
                return None
        return None


class ScraperAgent:
    """
    Two-phase scraper: Nimble extracts Place IDs + addresses at scale,
    Claude extracts qualitative venue signals from review text.
    """

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ScraperAgent":
        self._http = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def run(self, intent: VenueIntent) -> list[dict]:
        async with self:
            return await self._run(intent)

    async def _run(self, intent: VenueIntent) -> list[dict]:
        cuisine = intent.cuisine or "restaurant"
        city = intent.city
        occasion = intent.occasion
        city_unknown = city == "Unknown"

        # When city is unspecified, use location hints from other_signals or "near me"
        location = city if not city_unknown else (
            next((s for s in (intent.other_signals or [])
                  if any(kw in s.lower() for kw in ["city", "near", "in ", "at ", "nyc", "sf", "la", "chicago", "york", "angeles", "francisco"])),
                 "near me")
        )

        # ── Phase 1a: Nimble google_maps engine — structured local results + Place IDs
        if city_unknown:
            maps_query = f"best {cuisine} {occasion} restaurant {location}"
        else:
            maps_query = f"{cuisine} restaurant {city} {occasion}"
        maps_task = asyncio.create_task(self._nimble_maps_search(maps_query))

        # ── Phase 1b: Nimble google engine — organic SERP for review snippets
        if city_unknown:
            serp_queries = [
                f"best {cuisine} restaurant {occasion} group dining private room",
                f"top {cuisine} restaurant {occasion} special occasion {location}",
                f"site:reddit.com {cuisine} restaurant recommendation {occasion} group",
            ]
        else:
            serp_queries = [
                f"best {cuisine} restaurants {city} private room group dining",
                f"{cuisine} restaurant {city} {occasion} birthday reviews",
                f"site:reddit.com {cuisine} restaurant recommendation {city} {occasion}",
            ]
        serp_tasks = [asyncio.create_task(self._nimble_serp_search(q)) for q in serp_queries]

        # Run both phases in parallel
        maps_results, *serp_batches = await asyncio.gather(
            maps_task, *serp_tasks, return_exceptions=True
        )

        # ── Merge: maps results carry Place IDs; SERP results carry snippets
        maps_by_name: dict[str, NimbleMapsResult] = {}
        if isinstance(maps_results, list):
            maps_by_name = {_norm(r.name): r for r in maps_results}

        serp_raw: list[RawVenueResult] = []
        for batch in serp_batches:
            if isinstance(batch, list):
                serp_raw.extend(batch)

        # Merge SERP results with maps metadata using normalized name as key
        merged: list[RawVenueResult] = []
        for venue in self._deduplicate(serp_raw):
            maps_meta = maps_by_name.get(_norm(venue.name))
            if maps_meta:
                merged.append(venue.model_copy(update={
                    "place_id": maps_meta.place_id,
                    "address": maps_meta.address,
                    "latitude": maps_meta.latitude,
                    "longitude": maps_meta.longitude,
                }))
            else:
                merged.append(venue)

        # Also add maps-only results that didn't appear in SERP
        serp_names = {_norm(v.name) for v in merged}
        for maps_venue in (maps_by_name.values() if isinstance(maps_results, list) else []):
            if _norm(maps_venue.name) not in serp_names:
                merged.append(RawVenueResult(
                    name=maps_venue.name,
                    url=maps_venue.url,
                    snippet=maps_venue.snippet,
                    place_id=maps_venue.place_id,
                    address=maps_venue.address,
                    latitude=maps_venue.latitude,
                    longitude=maps_venue.longitude,
                ))

        # ── Phase 2: Claude signal extraction from snippets
        top = merged[:12]
        signals_list = await asyncio.gather(
            *[_call_with_retry(v) for v in top], return_exceptions=True
        )

        enriched: list[dict] = []
        for raw_venue, signals in zip(top, signals_list):
            if isinstance(signals, Exception) or signals is None:
                enriched.append(raw_venue.model_dump())
                continue
            # EnrichedVenue merges both models; place_id flows through from raw_venue
            ev = EnrichedVenue(**{**raw_venue.model_dump(), **signals.model_dump()})
            enriched.append(ev.model_dump())

        return enriched

    async def _nimble_maps_search(self, query: str) -> list[NimbleMapsResult]:
        """
        Phase 1a: Nimble google_maps engine.
        Uses computer vision to extract structured local-pack results including
        Google Place IDs, coordinates, ratings, and addresses.
        """
        assert self._http is not None
        with http_span(
            "therightspot.nimble_maps",
            "nimble",
            url=_NIMBLE_URL,
            **{"search.engine": "google_maps", "search.query": query[:100]},
        ) as span:
          try:
            resp = await self._http.post(
                _NIMBLE_URL,
                json={
                    "query": query,
                    "search_engine": "google_maps",
                    "country": "US",
                    "num_results": 15,
                    "render_js": False,
                    "parse": True,
                },
                headers={
                    "Authorization": f"Basic {NIMBLE_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            span.set_tag("http.status_code", resp.status_code)
            data = resp.json()
          except Exception:
            span.set_tag("error", True)
            return []

          results: list[NimbleMapsResult] = []
          for r in data.get("local_results", data.get("organic_results", [])):
            place_id = r.get("place_id", "") or _extract_place_id_from_url(r.get("link", ""))
            coords = r.get("gps_coordinates") or {}
            results.append(NimbleMapsResult(
                name=r.get("title", "").split(" - ")[0].strip(),
                place_id=place_id,
                address=r.get("address", ""),
                latitude=coords.get("latitude") or r.get("latitude"),
                longitude=coords.get("longitude") or r.get("longitude"),
                rating=r.get("rating"),
                review_count=r.get("reviews"),
                snippet=r.get("description", r.get("snippet", "")),
                url=r.get("link"),
                phone=r.get("phone"),
                business_type=r.get("type"),
            ))
          span.set_tag("results.count", len(results))
          return results

    async def _nimble_serp_search(self, query: str) -> list[RawVenueResult]:
        """
        Phase 1b: Nimble google engine.
        Standard SERP for organic results — review sites, Reddit, food blogs.
        """
        assert self._http is not None
        with http_span(
            "therightspot.nimble_serp",
            "nimble",
            url=_NIMBLE_URL,
            **{"search.engine": "google", "search.query": query[:100]},
        ) as span:
          try:
            resp = await self._http.post(
                _NIMBLE_URL,
                json={
                    "query": query,
                    "search_engine": "google",
                    "country": "US",
                    "num_results": 10,
                    "render_js": False,
                    "parse": True,
                },
                headers={
                    "Authorization": f"Basic {NIMBLE_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            span.set_tag("http.status_code", resp.status_code)
            data = resp.json()
          except Exception:
            span.set_tag("error", True)
            return []

          results = [
            RawVenueResult(
                name=r.get("title", "").split(" - ")[0].strip(),
                url=r.get("link"),
                snippet=r.get("snippet", ""),
                source=r.get("displayed_link", ""),
                place_id=_extract_place_id_from_url(r.get("link", "")),
            )
            for r in data.get("organic_results", [])
          ]
          span.set_tag("results.count", len(results))
          return results

    @staticmethod
    def _deduplicate(venues: list[RawVenueResult]) -> list[RawVenueResult]:
        seen: set[str] = set()
        unique: list[RawVenueResult] = []
        for v in venues:
            key = _norm(v.name)
            if len(key) > 2 and key not in seen:
                seen.add(key)
                unique.append(v)
        return unique


def _norm(name: str) -> str:
    return name.lower().replace("'", "").replace(" ", "")


def _extract_place_id_from_url(url: str | None) -> str:
    """Try to extract a Google Place ID from a Google Maps URL."""
    if not url:
        return ""
    m = _PLACE_ID_RE.search(url)
    return m.group(1) if m else ""
