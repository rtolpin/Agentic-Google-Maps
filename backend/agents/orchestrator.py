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

from tracing import ai_span, db_span, search_span
from .scraper_agent import ScraperAgent
from .validator_agent import ValidatorAgent
from .global_agent import GlobalIntelligenceAgent
from .publisher_agent import PublisherAgent
from db.clickhouse import ClickHouseClient
from db.redis_cache import RedisCache
from models.models import (
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

JSON schema (use null for unknown fields except city):
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

IMPORTANT: city must NEVER be null or "Unknown". If the query says "in the city", "near me",
or contains a city name anywhere (including appended at the end like "in New York City"), extract it.
If the query mentions a known city or major metro in any form (NYC, LA, SF, Chicago, etc.), normalize it
to the full name. If truly no city can be inferred, default to "New York City".

Examples:
- "birthday dinner for 8 quiet Italian NYC" →
  {"occasion":"birthday_dinner","group_size":8,"cuisine":"italian",
   "noise_preference":"quiet","needs_private_room":false,"city":"New York City",
   "date":null,"price_band":null,"dietary_restrictions":[],"other_signals":[]}
- "business lunch Tokyo private room 4 people" →
  {"occasion":"business_lunch","group_size":4,"cuisine":null,
   "noise_preference":"quiet","needs_private_room":true,"city":"Tokyo",
   "date":null,"price_band":"upscale","dietary_restrictions":[],"other_signals":[]}
- "birthday dinner for 8 in San Francisco" →
  {"occasion":"birthday_dinner","group_size":8,"cuisine":null,
   "noise_preference":null,"needs_private_room":false,"city":"San Francisco",
   "date":null,"price_band":null,"dietary_restrictions":[],"other_signals":[]}\
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


def _fallback_intelligence(venue: ScoredVenue, intent: VenueIntent) -> VenueIntelligence:
    """Build a basic intelligence card from key_quotes when Claude synthesis is unavailable."""
    occasion = intent.occasion or "your outing"
    cuisine = venue.cuisine or "venue"
    location = venue.neighborhood or venue.city or "the area"
    quotes = venue.key_quotes or []

    if quotes:
        why = f"{venue.name} is a {cuisine} spot in {location}. " + quotes[0]
    else:
        noise_desc = {"very_quiet": "very quiet", "quiet": "quiet", "moderate": "lively but comfortable",
                      "loud": "energetic", "very_loud": "very lively"}.get(venue.noise_level, "welcoming")
        why = (
            f"{venue.name} is a {noise_desc} {cuisine} in {location} that fits your criteria for {occasion}."
        )

    scenario = (
        f"Picture arriving at {venue.name} for {occasion} — "
        + (quotes[1] if len(quotes) > 1 else f"a great {cuisine} experience in {location}.")
    )

    score = int(min(100, max(0, venue.match_score)))
    return VenueIntelligence(
        why_card=why,
        scenario=scenario,
        sensitivity_bars={
            "ambiance": score,
            "privacy": 70 if venue.has_private_room else 40,
            "service": 65,
            "value": 60,
            "occasion_fit": score,
        },
        live_signal=None,
        suggestions=[
            f"What's the vibe like at {venue.name} on weekends?",
            f"Is {venue.name} good for {occasion}?",
            f"What should I order at {venue.name}?",
            f"How far is {venue.name} from the center of {venue.city}?",
        ],
    )


# ─── Orchestration ────────────────────────────────────────────────────────────

async def orchestrate(query: str, user_id: str, *, user_city: str | None = None) -> AsyncIterator[dict]:
    """
    Main orchestration loop.
    Yields SSE-compatible dicts as results arrive.
    Sub-agents run in parallel; ClickHouse blocking IO runs in a thread pool.
    """
    with search_span(query, user_id) as root:
        # Step 1 — intent parsing
        yield {"event": "status", "data": "Parsing your request..."}
        intent = await parse_intent(query)
        if intent.city == "Unknown" and user_city:
            intent = intent.model_copy(update={"city": user_city.strip()})
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

        # Fallback: ClickHouse empty (cold start / unknown city) — score enriched in-memory
        if not scored_venues and enriched_venues:
            scored_venues = _score_enriched_fallback(enriched_venues, intent)

        root.set_tag("search.venues_scored", len(scored_venues))

        # Step 6 — synthesize intelligence for top 5 (bounded by semaphore)
        intel_results = await asyncio.gather(
            *[synthesize_venue_intelligence(v, intent) for v in scored_venues[:5]],
            return_exceptions=True,
        )
        for venue, intel in zip(scored_venues[:5], intel_results):
            if isinstance(intel, VenueIntelligence):
                venue.intelligence = intel
            else:
                venue.intelligence = _fallback_intelligence(venue, intent)

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


def _score_enriched_fallback(enriched: list[dict], intent: VenueIntent) -> list[ScoredVenue]:
    """Score enriched venue dicts in-memory when ClickHouse returns nothing."""
    price_min, price_max = intent.price_range
    noise_pref = intent.noise_sql_value
    occasion = intent.occasion or ""
    results: list[ScoredVenue] = []
    for ev in enriched:
        name = ev.get("name") or ""
        if not name:
            continue
        city = ev.get("city") or intent.city
        score = 40.0  # base: passed all filters
        if intent.needs_private_room:
            score += 5 if ev.get("has_private_room") else 0
        max_group = ev.get("max_group_size") or 0
        if max_group == 0:
            score += 10  # unknown — partial credit
        elif max_group >= intent.group_size:
            score += 20
        else:
            score += max(0, 20.0 * max_group / max(1, intent.group_size))
        noise_raw = ev.get("noise_level") or "moderate"
        noise = noise_raw.value if hasattr(noise_raw, "value") else str(noise_raw)
        if noise_pref == "quiet":
            score += {"very_quiet": 15, "quiet": 12, "moderate": 5}.get(noise, 0)
        elif noise_pref == "lively":
            score += {"loud": 15, "very_loud": 12, "moderate": 7}.get(noise, 3)
        else:
            score += {"moderate": 12, "quiet": 9, "loud": 9}.get(noise, 6)
        birthday_score = min(100, (ev.get("birthday_mentions") or 0) * 10 + (50 if ev.get("birthday_friendly") else 0))
        occ_score = ev.get("special_occasion_score") or 0
        if occasion in ("birthday_dinner", "birthday_party"):
            score += (birthday_score * 0.20) if birthday_score > 0 else 8
        else:
            score += (occ_score * 0.20) if occ_score > 0 else 8
        price = ev.get("price_per_head_usd") or 0
        if price == 0:
            score += 5  # unknown price — partial credit
        elif price_min <= price <= price_max:
            score += 10
        score = min(100.0, max(0.0, score))
        venue_id = (name + city).lower().replace(" ", "_").replace("'", "")
        results.append(ScoredVenue(
            venue_id=venue_id,
            name=name,
            city=city,
            neighborhood=ev.get("neighborhood") or "",
            cuisine=ev.get("cuisine") or "",
            place_id=ev.get("place_id") or "",
            address=ev.get("address") or "",
            latitude=ev.get("latitude"),
            longitude=ev.get("longitude"),
            price_per_head=price,
            has_private_room=bool(ev.get("has_private_room")),
            max_group_size=max_group,
            noise_level=noise,
            birthday_score=birthday_score,
            key_quotes=ev.get("key_quotes") or [],
            match_score=round(score, 1),
        ))
    return sorted(results, key=lambda v: v.match_score, reverse=True)


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
