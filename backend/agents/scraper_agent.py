"""
Scraper Agent — two-phase data pipeline.

Phase 1 (Google Places Text Search API):
  - Searches for venues by natural-language query.
  - Returns Place IDs, coordinates, addresses, ratings, and editorial summaries.
  - Runs multiple parallel queries (broad + occasion-specific) for coverage.

Phase 2 (Claude):
  - Extracts structured venue signals (noise level, private room, capacity...)
    from the editorial summaries and any available review text.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import anthropic

from tracing import ai_span
from integrations.google_maps_client import GoogleMapsClient
from integrations.nimble_client import NimbleClient
from models.models import (
    EnrichedVenue,
    ExtractedSignals,
    RawVenueResult,
    VenueIntent,
)

_client = anthropic.AsyncAnthropic()
_CLAUDE_SEM = asyncio.Semaphore(5)

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


_OFFICE_OCCASIONS = {"offices", "office", "scouting offices", "corporate", "business"}

_CAFE_KEYWORDS = {"cafe", "café", "coffee", "cosy", "cozy", "laptop", "remote", "work from", "working"}
_WIFI_KEYWORDS = {"wifi", "wi-fi", "internet", "laptop"}

_OUTDOOR_KEYWORDS = {
    "hiking", "hike", "trail", "trails", "nature", "park", "outdoor", "outdoors",
    "walk", "walking", "trekking", "trek", "forest", "mountain", "mountains",
    "waterfall", "scenic", "wilderness", "campsite", "camping", "cycling", "bike trail",
    "greenway", "preserve", "state park", "national park",
}

def _build_queries(intent: VenueIntent) -> list[str]:
    """Build 2–3 complementary search queries for maximum venue coverage."""
    cuisine = intent.cuisine or ""
    city = intent.city
    occasion = intent.occasion.replace("_", " ").lower()
    signals = [s.lower() for s in (intent.other_signals or [])]
    all_terms = {occasion} | set(signals) | ({cuisine.lower()} if cuisine else set())

    if city == "Unknown":
        location = next(
            (s for s in (intent.other_signals or []) if len(s) > 3),
            "near me",
        )
    elif intent.neighborhood:
        location = f"{intent.neighborhood} {city}"
    else:
        location = city

    # Office / corporate HQ searches need entirely different queries
    is_office_search = (
        occasion in _OFFICE_OCCASIONS
        or any(kw in signals for kw in ("office", "headquarters", "hq", "corporate", "company"))
    )
    if is_office_search:
        named_companies = [s for s in signals if s not in ("office", "offices", "headquarters", "hq", "corporate", "company", "near")]
        if named_companies:
            company_str = " ".join(named_companies[:2])
            return [
                f"{company_str} headquarters office {location}",
                f"corporate headquarters office buildings {location}",
                f"tech company offices business district {location}",
            ]
        return [
            f"corporate headquarters major company offices {location}",
            f"tech company office buildings {location}",
            f"business district office towers {location}",
        ]

    # Outdoor / hiking / nature searches
    is_outdoor_search = any(kw in all_terms for kw in _OUTDOOR_KEYWORDS)
    if is_outdoor_search:
        # Include nearby regions so results aren't limited to city limits
        nearby = {
            "New York City": "New York New Jersey Hudson Valley",
            "Los Angeles": "Los Angeles Southern California",
            "San Francisco": "Bay Area Marin County",
            "Chicago": "Chicago Illinois Wisconsin",
            "Seattle": "Seattle Pacific Northwest",
            "Boston": "Boston New England",
            "Austin": "Austin Texas Hill Country",
            "Denver": "Denver Colorado Rockies",
            "Portland": "Portland Oregon Pacific Northwest",
        }
        region = nearby.get(city, city)
        activity = next((kw for kw in ("hiking", "trail", "walking", "cycling", "trekking") if kw in all_terms), "hiking trail")
        return [
            f"{activity} trails parks near {location}",
            f"best {activity} trails {region}",
            f"nature parks scenic trails day trips near {location}",
        ]

    # Café / remote-work / wifi searches
    has_wifi = any(kw in all_terms for kw in _WIFI_KEYWORDS)
    is_cafe_search = (
        any(kw in all_terms for kw in _CAFE_KEYWORDS)
        or cuisine.lower() in ("cafe", "café", "coffee", "coffee shop")
        or has_wifi
    )
    if is_cafe_search:
        wifi_tag = " with wifi" if has_wifi else ""
        return [
            f"cosy café coffee shop{wifi_tag} laptop friendly {location}",
            f"best café to work from{wifi_tag} {location}",
            f"quiet coffee shop{wifi_tag} {location}",
        ]

    # Detect specific non-restaurant venue types from occasion/signals
    _PUBLIC_VENUES = {
        "library", "libraries", "museum", "museums", "gallery", "galleries",
        "gym", "fitness", "pool", "swimming", "bowling", "cinema", "theatre",
        "theater", "arcade", "bookstore", "bookshop", "market", "farmers market",
        "spa", "salon", "pharmacy", "clinic", "hospital", "bank", "post office",
    }
    venue_type = cuisine or ""
    if not venue_type:
        venue_type = next((t for t in _PUBLIC_VENUES if t in all_terms), "")
    if not venue_type:
        # Extract from occasion if it names a place type directly
        venue_type = occasion if any(t in occasion for t in _PUBLIC_VENUES) else "restaurant"

    if venue_type != "restaurant" and venue_type not in {"dining", "dinner", "lunch", "brunch", "breakfast"}:
        return [
            f"{venue_type} {location}",
            f"best {venue_type} near {location}",
            f"{occasion} {venue_type} {location}",
        ]

    return [
        f"best {occasion} {venue_type} {location}",
        f"{venue_type} group dining {location}",
        f"special occasion {venue_type} {location}",
    ]


def _merge_snippet(google_snippet: str, nimble_snippet: str) -> str:
    """Combine Google editorial text and Nimble web snippet for richer Claude input."""
    parts = [s.strip() for s in (google_snippet, nimble_snippet) if s and s.strip()]
    return " | ".join(parts)


class ScraperAgent:
    """
    Two-phase scraper:
      Phase 1 — Google Places + Nimble run in parallel for maximum coverage.
                Google Places provides editorial summaries and price levels.
                Nimble google_maps adds Place IDs + local pack data from the
                open web; Nimble google_search adds Yelp/TripAdvisor snippets.
      Phase 2 — Claude extracts qualitative signals from the merged text.
    """

    async def run(
        self,
        intent: VenueIntent,
        *,
        user_lat: float | None = None,
        user_lng: float | None = None,
        user_radius_m: float | None = None,
    ) -> list[dict]:
        queries = _build_queries(intent)
        location = intent.neighborhood or intent.city
        bias = (
            {"lat": user_lat, "lng": user_lng, "radius_m": max(500.0, min(50000.0, user_radius_m or 5000.0))}
            if user_lat is not None and user_lng is not None else None
        )

        # ── Phase 1: Google Places (must complete) + Nimble (best-effort, 10s cap) ──
        async with GoogleMapsClient() as maps:
            google_tasks = [maps.search_venues(q, max_results=10, location_bias=bias) for q in queries]
            google_batches = await asyncio.gather(*google_tasks, return_exceptions=True)

        nimble_maps_batches: list[Any] = []
        nimble_serp_batches: list[Any] = []
        try:
            async with NimbleClient() as nimble:
                nimble_tasks = [
                    nimble.maps_search(queries[0], location),
                    *[nimble.serp_search(q) for q in queries[:2]],
                ]
                nimble_results = await asyncio.wait_for(
                    asyncio.gather(*nimble_tasks, return_exceptions=True),
                    timeout=10.0,
                )
            nimble_maps_batches = [nimble_results[0]]
            nimble_serp_batches = list(nimble_results[1:])
        except (asyncio.TimeoutError, Exception):
            pass  # Nimble is optional — proceed with Google Places only

        # Build a name→snippet index from Nimble SERP (organic web results)
        serp_snippets: dict[str, str] = {}
        for batch in nimble_serp_batches:
            if isinstance(batch, list):
                for r in batch:
                    name = r.get("name", "").lower()
                    if name:
                        serp_snippets[name] = r.get("snippet", "")

        # Merge and deduplicate venues across all sources
        # Priority: Google Places (has price level) → Nimble maps (has Place ID)
        seen_ids: set[str] = set()
        raw_venues: list[RawVenueResult] = []

        def _ingest(batch: Any, source_fallback: str) -> None:
            if isinstance(batch, Exception) or not isinstance(batch, list):
                return
            for v in batch:
                pid = v.get("place_id", "")
                key = pid or v.get("name", "").lower()
                if not key or key in seen_ids:
                    continue
                seen_ids.add(key)
                # Augment snippet with any matching Nimble SERP text
                name_key = v.get("name", "").lower()
                merged = _merge_snippet(
                    v.get("snippet", ""),
                    serp_snippets.get(name_key, ""),
                )
                raw_venues.append(RawVenueResult(
                    name=v.get("name", ""),
                    url=v.get("url"),
                    snippet=merged,
                    source=v.get("source", source_fallback),
                    place_id=pid,
                    address=v.get("address", ""),
                    latitude=v.get("latitude"),
                    longitude=v.get("longitude"),
                ))

        for batch in google_batches:
            _ingest(batch, "google_places")
        for batch in nimble_maps_batches:
            _ingest(batch, "nimble_maps")

        # ── Phase 2: Claude signal extraction ────────────────────────────────
        top = raw_venues[:12]
        signals_list = await asyncio.gather(
            *[_call_with_retry(v) for v in top], return_exceptions=True
        )

        # Price hint from Google Places API (most reliable source)
        places_price: dict[str, int] = {}
        for batch in google_batches:
            if isinstance(batch, list):
                for v in batch:
                    if v.get("place_id") and v.get("price_per_head_usd"):
                        places_price[v["place_id"]] = v["price_per_head_usd"]

        enriched: list[dict] = []
        for raw_venue, signals in zip(top, signals_list):
            base = raw_venue.model_dump()
            if isinstance(signals, Exception) or signals is None:
                enriched.append(base)
                continue
            sig_dict = signals.model_dump()
            if not sig_dict.get("price_per_head_usd") and raw_venue.place_id in places_price:
                sig_dict["price_per_head_usd"] = places_price[raw_venue.place_id]
            ev = EnrichedVenue(**{**base, **sig_dict})
            enriched.append(ev.model_dump())

        return enriched
