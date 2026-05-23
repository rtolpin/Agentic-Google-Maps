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


_OFFICE_OCCASIONS = {"offices", "office", "scouting offices", "work", "corporate", "business"}

def _build_queries(intent: VenueIntent) -> list[str]:
    """Build 2–3 complementary search queries for maximum venue coverage."""
    cuisine = intent.cuisine or "restaurant"
    city = intent.city
    occasion = intent.occasion.replace("_", " ")
    signals = [s.lower() for s in (intent.other_signals or [])]

    if city == "Unknown":
        location = next(
            (s for s in (intent.other_signals or []) if len(s) > 3),
            "near me",
        )
    else:
        location = city

    # Office / corporate HQ searches need entirely different queries
    is_office_search = (
        occasion.lower() in _OFFICE_OCCASIONS
        or any(kw in signals for kw in ("office", "headquarters", "hq", "corporate", "company"))
    )
    if is_office_search:
        # Look for specific named companies if mentioned
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

    return [
        f"best {occasion} {cuisine} restaurant {location}",
        f"{cuisine} restaurant group dining {location}",
        f"special occasion restaurant {location}",
    ]


class ScraperAgent:
    """
    Two-phase scraper: Google Places Text Search finds venues with Place IDs
    and coordinates; Claude extracts qualitative signals from editorial text.
    """

    async def run(self, intent: VenueIntent) -> list[dict]:
        queries = _build_queries(intent)

        async with GoogleMapsClient() as maps:
            tasks = [maps.search_venues(q, max_results=10) for q in queries]
            batches = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge and deduplicate by place_id
        seen_ids: set[str] = set()
        raw_venues: list[RawVenueResult] = []
        for batch in batches:
            if isinstance(batch, Exception) or not isinstance(batch, list):
                continue
            for v in batch:
                pid = v.get("place_id", "")
                key = pid or v.get("name", "")
                if key and key not in seen_ids:
                    seen_ids.add(key)
                    raw_venues.append(RawVenueResult(
                        name=v.get("name", ""),
                        url=v.get("url"),
                        snippet=v.get("snippet", ""),
                        source=v.get("source", "google_places"),
                        place_id=pid,
                        address=v.get("address", ""),
                        latitude=v.get("latitude"),
                        longitude=v.get("longitude"),
                    ))

        # Phase 2: Claude signal extraction from editorial summaries
        top = raw_venues[:12]
        signals_list = await asyncio.gather(
            *[_call_with_retry(v) for v in top], return_exceptions=True
        )

        # Merge price hint from Places API into extracted signals
        places_price: dict[str, int] = {}
        for batch in batches:
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
            # Use Places API price if Claude didn't extract one
            if not sig_dict.get("price_per_head_usd") and raw_venue.place_id in places_price:
                sig_dict["price_per_head_usd"] = places_price[raw_venue.place_id]
            ev = EnrichedVenue(**{**base, **sig_dict})
            enriched.append(ev.model_dump())

        return enriched
