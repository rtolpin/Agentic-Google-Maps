"""
The Right Spot — Orchestrator Agent
Parses intent, dispatches sub-agents in parallel, synthesizes intelligence,
and streams SSE-compatible results back to the client.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import AsyncIterator

import anthropic

from ..tracing import ai_span, db_span, search_span
from .scraper_agent import ScraperAgent
from .validator_agent import ValidatorAgent
from .global_agent import GlobalIntelligenceAgent
from .publisher_agent import PublisherAgent
from ..db.clickhouse import ClickHouseClient
from ..db.redis_cache import RedisCache
from ..models.models import (
    ScoredVenue,
    UserPreferences,
    VenueIntelligence,
    VenueIntent,
)

# AsyncAnthropic — never blocks the event loop
_client = anthropic.AsyncAnthropic()
_ch = ClickHouseClient()
_cache = RedisCache()

# Limit simultaneous synthesis calls so we don't hammer the API rate limit
_SYNTHESIS_SEM = asyncio.Semaphore(3)


# ─── System Prompts ───────────────────────────────────────────────────────────

_INTENT_PARSER_PROMPT = """\
You are an intent parser for a venue discovery system.
Extract structured intent from the user's natural language query.
Respond ONLY with valid JSON. No preamble, no markdown fences.

JSON schema (use null for unknown fields):
{
  "occasion": string,
  "group_size": int,
  "cuisine": string | null,
  "noise_preference": "quiet" | "moderate" | "lively" | null,
  "needs_private_room": bool,
  "city": string,
  "date": string | null,
  "price_band": "budget" | "mid" | "upscale" | "luxury" | null,
  "dietary_restrictions": [string],
  "other_signals": [string]
}

Examples:
- "birthday dinner for 8 quiet Italian NYC" →
  {"occasion":"birthday_dinner","group_size":8,"cuisine":"italian",
   "noise_preference":"quiet","needs_private_room":false,"city":"New York City",
   "date":null,"price_band":null,"dietary_restrictions":[],"other_signals":[]}
- "business lunch Tokyo private room 4 people" →
  {"occasion":"business_lunch","group_size":4,"cuisine":null,
   "noise_preference":"quiet","needs_private_room":true,"city":"Tokyo",
   "date":null,"price_band":"upscale","dietary_restrictions":[],"other_signals":[]}\
"""

_SYNTHESIS_PROMPT = """\
You are The Right Spot's intelligence engine.
Given a scored venue and the user's search intent, produce a structured intelligence card.

Output ONLY valid JSON with exactly these keys:
{
  "why_card": string,           // 2-sentence plain-English fit explanation
  "scenario": string,           // hyper-realistic simulation of the user's evening
  "sensitivity_bars": {         // dimension → score 0-100
    "ambiance": int,
    "privacy": int,
    "service": int,
    "value": int,
    "occasion_fit": int
  },
  "live_signal": string | null, // urgency alert (e.g. "Books out 3 weeks ahead") or null
  "suggestions": [string]       // exactly 4 follow-up questions the user might ask
}\
"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _cache_key(prefix: str, text: str) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _extract_json(text: str) -> dict:
    """Parse JSON from model output, stripping markdown fences if present."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return json.loads(m.group(1).strip())
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"No JSON found in model output: {text[:300]}")


# ─── Agent Calls ──────────────────────────────────────────────────────────────

async def parse_intent(query: str) -> VenueIntent:
    """Parse natural language into a validated VenueIntent. Cached for 1 hour."""
    with ai_span("therightspot.parse_intent", query_length=len(query)) as span:
        key = _cache_key("intent", query)
        cached = await _cache.get(key)
        if cached:
            span.set_tag("cache.hit", True)
            return VenueIntent.model_validate_json(cached)

        span.set_tag("cache.hit", False)
        response = await _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=[{
                "type": "text",
                "text": _INTENT_PARSER_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": query}],
        )
        span.set_tag("tokens.input", response.usage.input_tokens)
        span.set_tag("tokens.output", response.usage.output_tokens)
        intent = VenueIntent.model_validate(_extract_json(response.content[0].text))
        span.set_tag("intent.occasion", intent.occasion)
        span.set_tag("intent.city", intent.city)
        await _cache.set(key, intent.model_dump_json(), ttl=3600)
        return intent


async def synthesize_venue_intelligence(
    venue: ScoredVenue, intent: VenueIntent
) -> VenueIntelligence:
    """Generate why-card, scenario, and sensitivity bars for one venue."""
    with ai_span(
        "therightspot.synthesize",
        venue_id=venue.venue_id,
        venue_name=venue.name,
        match_score=venue.match_score,
    ) as span:
        async with _SYNTHESIS_SEM:
            prompt = (
                f"Intent: {intent.model_dump_json()}\n"
                f"Venue: {venue.model_dump_json(exclude={'intelligence'})}\n\n"
                "Generate the intelligence card."
            )
            response = await _client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=[{
                    "type": "text",
                    "text": _SYNTHESIS_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
            )
            span.set_tag("tokens.input", response.usage.input_tokens)
            span.set_tag("tokens.output", response.usage.output_tokens)
            return VenueIntelligence.model_validate(_extract_json(response.content[0].text))


# ─── Orchestration ────────────────────────────────────────────────────────────

async def orchestrate(query: str, user_id: str) -> AsyncIterator[dict]:
    """
    Main orchestration loop.
    Yields SSE-compatible dicts as results arrive.
    Sub-agents run in parallel; ClickHouse blocking IO runs in a thread pool.
    """
    with search_span(query, user_id) as root:
        # Step 1 — intent parsing
        yield {"event": "status", "data": "Parsing your request..."}
        intent = await parse_intent(query)
        yield {"event": "intent", "data": intent.model_dump()}

        # Step 2 — warm cache check (non-blocking thread)
        with db_span("therightspot.cache_check", "venue_signals", city=intent.city):
            cached_venues = await asyncio.to_thread(
                _ch.get_cached_scores, intent.city, intent.cuisine or ""
            )

        # Step 3 — dispatch sub-agents in parallel
        yield {"event": "status", "data": "Searching across sources..."}
        scrape_task = asyncio.create_task(ScraperAgent().run(intent))
        validate_task = asyncio.create_task(ValidatorAgent().run(intent))
        global_task = asyncio.create_task(GlobalIntelligenceAgent().run(intent))

        pending = {scrape_task, validate_task, global_task}
        enriched_venues: list[dict] = []

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = await task
                if task is scrape_task:
                    enriched_venues = result
                    yield {"event": "venues_raw", "data": enriched_venues}
                elif task is validate_task:
                    yield {"event": "validation", "data": result}
                elif task is global_task:
                    yield {"event": "global_intel", "data": result}

        # Step 4 — persist signals (thread pool; non-critical path)
        with db_span("therightspot.upsert_venues", "venue_signals", city=intent.city,
                     row_count=len(enriched_venues)):
            await asyncio.to_thread(_ch.upsert_venue_signals, enriched_venues, intent.city)

        # Step 5 — score venues
        yield {"event": "status", "data": "Ranking matches..."}
        with db_span("therightspot.score_venues", "venue_signals", city=intent.city) as span:
            scored_venues: list[ScoredVenue] = await asyncio.to_thread(_ch.score_venues, intent)
            span.set_tag("db.rows_returned", len(scored_venues))

        root.set_tag("search.venues_scored", len(scored_venues))

        # Step 6 — synthesize intelligence for top 3 (bounded by semaphore)
        intel_results = await asyncio.gather(
            *[synthesize_venue_intelligence(v, intent) for v in scored_venues[:3]],
            return_exceptions=True,
        )
        for venue, intel in zip(scored_venues[:3], intel_results):
            if isinstance(intel, VenueIntelligence):
                venue.intelligence = intel

        # Step 7 — personalization re-rank
        raw_prefs = await _cache.get_user_prefs(user_id)
        if raw_prefs:
            prefs = UserPreferences.model_validate(raw_prefs)
            scored_venues = _apply_personalization(scored_venues, prefs)
            root.set_tag("search.personalized", True)

        yield {"event": "results", "data": [v.model_dump() for v in scored_venues]}

        # Step 8 — fire-and-forget guide publish
        asyncio.create_task(PublisherAgent().publish_guide(intent, scored_venues[:5]))

        yield {"event": "done", "data": {"total_venues": len(scored_venues)}}


def _apply_personalization(
    venues: list[ScoredVenue], prefs: UserPreferences
) -> list[ScoredVenue]:
    """Boost scores based on user's historical preferences, then re-sort."""
    for venue in venues:
        # If noise is a dealbreaker, skip all positive boosts for this venue
        if prefs.prefers_quiet and venue.noise_level in ("loud", "very_loud"):
            continue
        boost = 0.0
        if prefs.prefers_quiet and venue.noise_level in ("very_quiet", "quiet"):
            boost += 12
        if prefs.prefers_private_room and venue.has_private_room:
            boost += 15
        if venue.cuisine and venue.cuisine in prefs.preferred_cuisines:
            boost += 12
        if venue.neighborhood and venue.neighborhood in prefs.preferred_neighborhoods:
            boost += 8
        if prefs.price_ceiling and venue.price_per_head <= prefs.price_ceiling:
            boost += 5
        venue.match_score = min(100.0, venue.match_score + boost)
    return sorted(venues, key=lambda v: v.match_score, reverse=True)
