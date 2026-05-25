"""
The Right Spot — Orchestrator Agent
Parses intent, dispatches sub-agents in parallel, synthesizes intelligence,
and streams SSE-compatible results back to the client.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from typing import AsyncIterator

import anthropic

from tracing import ai_span, db_span, search_span
from .scraper_agent import ScraperAgent, _CITY_COORDS
from integrations.google_maps_client import GoogleMapsClient
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
  "neighborhood": string | null,
  "date": string | null,
  "price_band": "budget" | "mid" | "upscale" | "luxury" | null,
  "dietary_restrictions": [string],
  "other_signals": [string]
}

CUISINE RULE — CRITICAL: When a specific food item is mentioned, use the FOOD ITEM as cuisine,
NOT a broad cuisine category. Examples:
  "best pancakes" → cuisine="pancakes" (NOT "american")
  "best tacos" → cuisine="tacos" (NOT "mexican")
  "best sushi" → cuisine="sushi" (NOT "japanese")
  "best pizza" → cuisine="pizza" (NOT "italian")
  "best ramen" → cuisine="ramen" (NOT "japanese")
  "best burgers" → cuisine="burgers" (NOT "american")
Only use a broad category (american, italian, etc.) when NO specific dish is mentioned
and the user explicitly says "Italian restaurant", "Mexican food", etc.

CITY RULE: city must NEVER be null or "Unknown". If the query says "in the city", "near me",
or contains a city name anywhere (including appended at the end like "in New York City"), extract it.
If the query mentions a known city or major metro in any form (NYC, LA, SF, Chicago, etc.), normalize it
to the full name. For suburbs and small towns (e.g. "North Caldwell", "Maplewood", "Hoboken",
"Montclair", "Ridgewood"), always extract the exact town name as city — do NOT substitute a
nearby major city. If truly no city can be inferred, default to "New York City".

neighborhood: Extract sub-city areas like "Upper East Side", "SoHo", "Williamsburg", "Brooklyn",
"DUMBO", "Mission District", "Lower East Side", "West Village", "Chelsea", "Midtown", "FiDi",
"South of Market", "Capitol Hill", etc. These are distinct from city. Set to null if no
sub-area is mentioned.

Examples:
- "birthday dinner for 8 quiet Italian NYC" →
  {"occasion":"birthday_dinner","group_size":8,"cuisine":"italian",
   "noise_preference":"quiet","needs_private_room":false,"city":"New York City",
   "neighborhood":null,"date":null,"price_band":null,"dietary_restrictions":[],"other_signals":[]}
- "best pancakes in North Caldwell" →
  {"occasion":"dining","group_size":1,"cuisine":"pancakes",
   "noise_preference":null,"needs_private_room":false,"city":"North Caldwell",
   "neighborhood":null,"date":null,"price_band":null,"dietary_restrictions":[],"other_signals":[]}
- "best tacos Upper East Side" →
  {"occasion":"dining","group_size":1,"cuisine":"tacos",
   "noise_preference":null,"needs_private_room":false,"city":"New York City",
   "neighborhood":"Upper East Side","date":null,"price_band":null,"dietary_restrictions":[],"other_signals":[]}
- "business lunch Tokyo private room 4 people" →
  {"occasion":"business_lunch","group_size":4,"cuisine":null,
   "noise_preference":"quiet","needs_private_room":true,"city":"Tokyo",
   "neighborhood":null,"date":null,"price_band":"upscale","dietary_restrictions":[],"other_signals":[]}
- "restaurants on the Upper East Side" →
  {"occasion":"dining","group_size":2,"cuisine":null,
   "noise_preference":null,"needs_private_room":false,"city":"New York City",
   "neighborhood":"Upper East Side","date":null,"price_band":null,"dietary_restrictions":[],"other_signals":[]}
- "furniture stores in Brooklyn" →
  {"occasion":"shopping","group_size":1,"cuisine":null,
   "noise_preference":null,"needs_private_room":false,"city":"New York City",
   "neighborhood":"Brooklyn","date":null,"price_band":null,"dietary_restrictions":[],"other_signals":["furniture"]}\
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
}

CRITICAL — grounding rule: never invent or assert specific menu items, dishes, or prices
that do not appear in the venue's key_quotes or the user's search query.
If a specific food item was searched (e.g. "pancakes", "tacos") but does NOT appear in
the venue's key_quotes, describe the venue's cuisine and atmosphere instead — do NOT
claim the venue serves that item. Write around the gap naturally.\
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
    """Build a basic intelligence card from venue attributes and key_quotes."""
    occasion = intent.occasion or "your outing"
    cuisine = venue.cuisine or "venue"
    location = venue.neighborhood or venue.city or "the area"
    quotes = venue.key_quotes or []
    noise_desc = {"very_quiet": "very quiet", "quiet": "quiet", "moderate": "lively but comfortable",
                  "loud": "energetic", "very_loud": "very lively"}.get(venue.noise_level, "welcoming")
    price_hint = f" at around ${venue.price_per_head}/head" if venue.price_per_head else ""
    room_hint = " with a private room available" if venue.has_private_room else ""

    if quotes:
        why = f"{venue.name} is a {noise_desc} {cuisine} in {location}{price_hint}{room_hint}. {quotes[0]}"
    else:
        why = (
            f"{venue.name} is a {noise_desc} {cuisine} in {location}{price_hint}{room_hint}. "
            f"It scored {round(venue.match_score)}% for {occasion} based on atmosphere, capacity, and occasion fit."
        )

    scenario = (
        f"Picture arriving at {venue.name} for {occasion} — "
        + (quotes[1] if len(quotes) > 1 else f"a {noise_desc} {cuisine} experience in {location}{price_hint}.")
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

async def orchestrate(
    query: str,
    user_id: str,
    *,
    user_city: str | None = None,
    user_lat: float | None = None,
    user_lng: float | None = None,
    user_radius_m: float | None = None,
) -> AsyncIterator[dict]:
    """
    Main orchestration loop.
    Yields SSE-compatible dicts as results arrive.
    Sub-agents run in parallel; ClickHouse blocking IO runs in a thread pool.
    """
    with search_span(query, user_id) as root:
        # Step 1 — intent parsing
        yield {"event": "status", "data": "Parsing your request..."}
        intent = await parse_intent(query)
        # When GPS coords are present, user_city (from the frontend geocoder) is the
        # authoritative city name — it comes from the same coordinates being searched.
        # Apply it now so the GPS reverse-geocode block below can refine neighborhood/county
        # on top of it, rather than fighting the LLM default "New York City".
        if user_city and user_lat is not None and user_lng is not None:
            intent = intent.model_copy(update={"city": user_city.strip()})
        elif intent.city in ("Unknown", "") and user_city:
            intent = intent.model_copy(update={"city": user_city.strip()})

        # GPS override: reverse geocode → hyper-local area string (suburb/rural support)
        # This runs whenever coordinates are provided, including Search This Area.
        user_area = ""
        if user_lat is not None and user_lng is not None:
            try:
                async with GoogleMapsClient() as gc:
                    geo_parts = await gc.reverse_geocode(user_lat, user_lng)
                if geo_parts:
                    city_name    = geo_parts.get("city") or geo_parts.get("county", "")
                    neighborhood = geo_parts.get("neighborhood", "")
                    county       = geo_parts.get("county", "")
                    state        = geo_parts.get("state", "")
                    # Discard intersection-style names like "Greenwood & Hamilton"
                    # — Google Places returns 0 results when they appear in queries.
                    if neighborhood and ("&" in neighborhood or neighborhood[0].isdigit()):
                        neighborhood = ""
                    primary      = neighborhood or city_name
                    area_parts   = [p for p in [primary, county, state] if p]
                    user_area    = ", ".join(area_parts)
                    updates: dict = {}
                    if city_name:
                        updates["city"] = city_name
                    if neighborhood:
                        updates["neighborhood"] = neighborhood
                    if updates:
                        intent = intent.model_copy(update=updates)
            except Exception:
                pass

        yield {"event": "intent", "data": intent.model_dump()}

        # Step 1b — geocode unknown cities ONCE so both the scraper and the location
        # filter use identical coordinates.  Without this, two independent geocoding
        # calls can diverge (or one fails) causing cross-state venues to appear on
        # repeat searches when the ClickHouse cache is warm.
        #
        # Validation: geocoding "North Caldwell" (NJ) can return "N. Caldwell St,
        # Charlotte, NC" because Google interprets it as a street name.  We check
        # that all words of the queried city appear in the formatted_address; if not
        # we discard the result and let the scraper do a text-only query instead.
        city_geocode: tuple[float, float] | None = None
        if user_lat is None and intent.city not in ("Unknown", "") \
                and intent.city.strip() not in _CITY_COORDS:
            try:
                async with GoogleMapsClient() as gc:
                    geo = await gc.geocode(intent.city)
                    if geo and _geocode_matches_city(intent.city, geo.formatted_address):
                        city_geocode = (geo.latitude, geo.longitude)
                    elif not city_geocode:
                        # First attempt returned a non-matching result (e.g. "North Caldwell"
                        # → "N. Caldwell St, Charlotte, NC").  Retry with ", USA" qualifier —
                        # this resolves US suburb names that are also street names elsewhere.
                        geo2 = await gc.geocode(f"{intent.city}, USA")
                        if geo2 and _geocode_matches_city(intent.city, geo2.formatted_address):
                            city_geocode = (geo2.latitude, geo2.longitude)
            except Exception:
                pass

        # Step 2 — warm cache check (non-blocking thread)
        with db_span("therightspot.cache_check", "venue_signals", city=intent.city):
            cached_venues = await asyncio.to_thread(
                _ch.get_cached_scores, intent.city, intent.cuisine or ""
            )

        # Step 3 — dispatch sub-agents in parallel
        yield {"event": "status", "data": "Searching across sources..."}
        scrape_task = asyncio.create_task(ScraperAgent().run(
            intent,
            user_lat=user_lat, user_lng=user_lng,
            user_radius_m=user_radius_m, user_area=user_area,
            city_lat=city_geocode[0] if city_geocode else None,
            city_lng=city_geocode[1] if city_geocode else None,
        ))
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
            await asyncio.to_thread(_ch.upsert_venue_signals, enriched_venues, intent.city, intent.cuisine or "")

        # If the city geocode was rejected during validation, derive an anchor from the
        # fresh scrape results.  Uses median (not mean) so a single far-away outlier
        # (e.g. Charlotte NC appearing in a text-only "North Caldwell" search) doesn't
        # drag the anchor hundreds of miles away and filter out the correct NJ results.
        if city_geocode is None and enriched_venues:
            _coords = sorted(
                [(v["latitude"], v["longitude"])
                 for v in enriched_venues if v.get("latitude") and v.get("longitude")],
                key=lambda c: c[0],
            )
            if _coords:
                mid = len(_coords) // 2
                city_geocode = _coords[mid]

        # Step 5 — score venues
        yield {"event": "status", "data": "Ranking matches..."}
        with db_span("therightspot.score_venues", "venue_signals", city=intent.city) as span:
            scored_venues: list[ScoredVenue] = await asyncio.to_thread(_ch.score_venues, intent)
            span.set_tag("db.rows_returned", len(scored_venues))

        # Filter BEFORE fallback check — ClickHouse may hold stale entries with wrong
        # coordinates (stored before the locationRestriction fix).  Filtering first means
        # "all ClickHouse results were stale" correctly triggers the in-memory fallback.
        scored_venues = await _filter_by_location(
            scored_venues, intent, user_lat=user_lat, user_lng=user_lng,
            user_radius_m=user_radius_m, city_geocode=city_geocode,
        )

        # Fallback: no valid results in ClickHouse → score fresh scraper data in-memory
        if not scored_venues and enriched_venues:
            scored_venues = _score_enriched_fallback(enriched_venues, intent)
            scored_venues = await _filter_by_location(
                scored_venues, intent, user_lat=user_lat, user_lng=user_lng,
                user_radius_m=user_radius_m, city_geocode=city_geocode,
            )

        root.set_tag("search.venues_scored", len(scored_venues))

        # Step 6 — synthesize intelligence for top 10 (bounded by semaphore)
        intel_results = await asyncio.gather(
            *[synthesize_venue_intelligence(v, intent) for v in scored_venues[:10]],
            return_exceptions=True,
        )
        for venue, intel in zip(scored_venues[:10], intel_results):
            if isinstance(intel, VenueIntelligence):
                venue.intelligence = intel
            else:
                venue.intelligence = _fallback_intelligence(venue, intent)
        # Venues beyond top 10 always get a fallback card so descriptions are never blank
        for venue in scored_venues[10:]:
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


def _geocode_matches_city(city: str, formatted_address: str) -> bool:
    """Return True if every word in city appears in the geocoded formatted_address.

    Prevents accepting geocode results that point to a completely different place,
    e.g. geocoding "North Caldwell" returning "N. Caldwell St, Charlotte, NC" where
    "north" is absent — the distance anchor would then be in the wrong state.
    """
    city_words = city.lower().split()
    addr_lower = formatted_address.lower()
    return all(w in addr_lower for w in city_words)


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def _filter_by_location(
    venues: list[ScoredVenue],
    intent: VenueIntent,
    *,
    user_lat: float | None,
    user_lng: float | None,
    user_radius_m: float | None,
    city_geocode: tuple[float, float] | None = None,
) -> list[ScoredVenue]:
    """Remove venues whose coordinates fall outside the expected search area.

    city_geocode: pre-computed (lat, lng) for the intent city, geocoded ONCE
    in the orchestrator so the scraper and both filter calls share the same anchor.
    Passing it here avoids a second independent geocoding call that could fail or
    return different coordinates and let cross-state venues through.
    """
    if user_lat is not None and user_lng is not None:
        clat, clng = user_lat, user_lng
        max_m = max(500.0, min(50000.0, user_radius_m or 5000.0)) * 2
    elif intent.city not in ("Unknown", ""):
        city_key = intent.city.strip()
        if city_key in _CITY_COORDS:
            clat, clng, _ = _CITY_COORDS[city_key]
            max_m = 35_000.0  # major city — generous metro radius
        elif city_geocode is not None:
            # Use the pre-geocoded coordinates — no second API call needed.
            clat, clng = city_geocode
            max_m = 20_000.0  # 20 km: covers adjacent towns, excludes distant cities
        else:
            # No pre-geocoded coords available — geocode as last resort.
            # Fail closed (return []) rather than fail open (return venues): stale
            # ClickHouse entries tagged with the wrong city would otherwise bypass
            # the distance filter entirely.
            try:
                async with GoogleMapsClient() as gc:
                    geo = await gc.geocode(intent.city)
                    if not geo or not _geocode_matches_city(intent.city, geo.formatted_address):
                        geo = await gc.geocode(f"{intent.city}, USA")
                if not geo or not _geocode_matches_city(intent.city, geo.formatted_address):
                    return []
                clat, clng = geo.latitude, geo.longitude
            except Exception:
                return []
            max_m = 20_000.0
    else:
        return venues

    filtered = []
    is_gps_search = user_lat is not None and user_lng is not None
    for v in venues:
        if v.latitude is None or v.longitude is None:
            # No usable coordinates (stored as 0.0 sentinel or never set).
            # GPS searches: keep — venue shows in list without a map pin.
            # Named-city searches: drop — stale ClickHouse entries from
            # wrong-location scrapes (e.g. a Charlotte Nimble result tagged
            # city="North Caldwell") would bypass distance filtering entirely.
            if is_gps_search:
                filtered.append(v)
            continue
        if _haversine_m(clat, clng, v.latitude, v.longitude) <= max_m:
            filtered.append(v)
    return filtered


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
        score = 25.0  # base: passed all filters
        if intent.needs_private_room:
            score += 5 if ev.get("has_private_room") else 0
        max_group = ev.get("max_group_size") or 0
        if max_group == 0:
            score += 5  # unknown — minimal partial credit
        elif max_group >= intent.group_size:
            score += 20
        else:
            score += max(0, 20.0 * max_group / max(1, intent.group_size))
        noise_raw = ev.get("noise_level") or ""
        noise = noise_raw.value if hasattr(noise_raw, "value") else str(noise_raw)
        if noise_pref == "quiet":
            score += {"very_quiet": 15, "quiet": 12, "moderate": 5}.get(noise, 0)
        elif noise_pref == "lively":
            score += {"loud": 15, "very_loud": 12, "moderate": 7}.get(noise, 3)
        else:
            score += {"moderate": 12, "quiet": 9, "loud": 9}.get(noise, 3)
        birthday_score = min(100, (ev.get("birthday_mentions") or 0) * 10 + (50 if ev.get("birthday_friendly") else 0))
        occ_score = ev.get("special_occasion_score") or 0
        if occasion in ("birthday_dinner", "birthday_party"):
            score += (birthday_score * 0.25) if birthday_score > 0 else 4
        else:
            score += (occ_score * 0.25) if occ_score > 0 else 4
        price = ev.get("price_per_head_usd") or 0
        if price == 0:
            score += 3  # unknown price — minimal partial credit
        elif price_min <= price <= price_max:
            score += 15

        # Google Places rating bonus (0-40 pts)
        rating = ev.get("google_rating") or 0.0
        score += round(max(0.0, min(40.0, (rating - 2.0) * 13.3)), 1)

        # Cuisine keyword match bonus:
        #   +15 pts when the specific food item appears in name/snippet
        #         ("Pancake House", "known for their pancakes")
        #   +3 pts when only the broader category appears — weak signal, not the dish
        #         (a generic "breakfast diner" for a pancakes search)
        if intent.cuisine:
            from agents.scraper_agent import _FOOD_TO_CATEGORY
            cuisine_kw = intent.cuisine.lower()
            food_cat = _FOOD_TO_CATEGORY.get(cuisine_kw, "")
            text = name.lower() + " " + (ev.get("snippet") or "").lower()
            if cuisine_kw in text:
                score += 15
            elif food_cat and food_cat in text:
                score += 3

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
