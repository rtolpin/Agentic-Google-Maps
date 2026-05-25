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
import math
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

_OPEN_NOW_KEYWORDS = {"open now", "open right now", "currently open"}
# "open today" / "open this weekend" mean "operating today", not "open at this exact moment".
# Those are handled by including them in query text rather than setting openNow=true,
# which would return 0 results when searched outside business hours.
_OPEN_TODAY_KEYWORDS = {"open today", "open this weekend", "open this week"}

# Maps specific food items to their restaurant-category search term.
# Used in query building to add a broader category query alongside the
# specific-dish query — e.g. "pancakes" → also search "breakfast restaurant".
# This catches places that serve the dish but don't have it in their name.
_FOOD_TO_CATEGORY: dict[str, str] = {
    # Breakfast / brunch
    "pancakes": "breakfast",
    "waffles": "breakfast",
    "french toast": "breakfast",
    "eggs benedict": "breakfast",
    "omelette": "breakfast",
    "omelettes": "breakfast",
    "crepes": "breakfast",
    "granola": "breakfast",
    "acai bowl": "breakfast",
    "bagels": "bagel shop",
    "donuts": "bakery",
    "doughnuts": "bakery",
    "pastries": "bakery",
    "croissants": "bakery",
    # Pizza / Italian
    "pizza": "pizzeria",
    "pasta": "italian",
    "lasagna": "italian",
    "risotto": "italian",
    "carbonara": "italian",
    "tiramisu": "italian",
    # Japanese
    "sushi": "japanese",
    "sashimi": "japanese",
    "ramen": "ramen shop",
    "udon": "japanese",
    "tempura": "japanese",
    "yakitori": "japanese",
    "izakaya": "japanese",
    "omakase": "japanese",
    # Chinese
    "dumplings": "chinese",
    "dim sum": "chinese",
    "bao": "chinese",
    "fried rice": "chinese",
    "peking duck": "chinese",
    "hot pot": "chinese",
    # Korean
    "korean bbq": "korean",
    "bibimbap": "korean",
    "bulgogi": "korean",
    "kimchi": "korean",
    # Vietnamese
    "pho": "vietnamese",
    "banh mi": "vietnamese",
    "spring rolls": "vietnamese",
    # Mexican
    "tacos": "mexican",
    "burritos": "mexican",
    "enchiladas": "mexican",
    "quesadillas": "mexican",
    "tamales": "mexican",
    "nachos": "mexican",
    "guacamole": "mexican",
    # American / BBQ
    "burgers": "burger joint",
    "cheeseburger": "burger joint",
    "wings": "american",
    "chicken wings": "american",
    "fried chicken": "american",
    "bbq": "barbecue",
    "ribs": "barbecue",
    "brisket": "barbecue",
    "pulled pork": "barbecue",
    "steak": "steakhouse",
    "cheesesteak": "american",
    # Seafood
    "seafood": "seafood",
    "lobster": "seafood",
    "oysters": "seafood",
    "clams": "seafood",
    "crab": "seafood",
    "fish and chips": "seafood",
    # Indian / South Asian
    "curry": "indian",
    "biryani": "indian",
    "tikka masala": "indian",
    "naan": "indian",
    "samosas": "indian",
    # Middle Eastern / Mediterranean
    "falafel": "middle eastern",
    "shawarma": "middle eastern",
    "kebab": "middle eastern",
    "hummus": "mediterranean",
    "gyros": "greek",
    "pita": "mediterranean",
    # Sandwiches / Deli
    "sandwiches": "deli",
    "pastrami": "deli",
    "hoagies": "deli",
    # Healthy / other
    "salad": "healthy",
    "smoothies": "juice bar",
    "acai": "healthy",
    "poke": "hawaiian",
    "ice cream": "ice cream",
    "gelato": "ice cream",
    "frozen yogurt": "ice cream",
    "churros": "dessert",
    "crepe": "french",
    "fondue": "european",
    "wonton": "chinese",
    "noodles": "asian",
}


_CLAUDE_EXTRACTION_LIMIT = 20  # venues that get full Claude signal extraction
_MAX_VENUES = 20               # return up to 20 recommendations

# Hardcoded city coordinates — used before geocoding so a geocoding failure
# never silently disables the location restriction.
# (lat, lng, ISO-3166-1 alpha-2 country code)
_CITY_COORDS: dict[str, tuple[float, float, str]] = {
    "New York City":  (40.7128, -74.0060, "US"),
    "New York":       (40.7128, -74.0060, "US"),
    "NYC":            (40.7128, -74.0060, "US"),
    "Manhattan":      (40.7831, -73.9712, "US"),
    "Brooklyn":       (40.6782, -73.9442, "US"),
    "Queens":         (40.7282, -73.7949, "US"),
    "Bronx":          (40.8448, -73.8648, "US"),
    "Los Angeles":    (34.0522, -118.2437, "US"),
    "LA":             (34.0522, -118.2437, "US"),
    "San Francisco":  (37.7749, -122.4194, "US"),
    "SF":             (37.7749, -122.4194, "US"),
    "Chicago":        (41.8781, -87.6298, "US"),
    "Seattle":        (47.6062, -122.3321, "US"),
    "Boston":         (42.3601, -71.0589, "US"),
    "Austin":         (30.2672, -97.7431, "US"),
    "Denver":         (39.7392, -104.9903, "US"),
    "Portland":       (45.5051, -122.6750, "US"),
    "Miami":          (25.7617, -80.1918, "US"),
    "Atlanta":        (33.7490, -84.3880, "US"),
    "Dallas":         (32.7767, -96.7970, "US"),
    "Houston":        (29.7604, -95.3698, "US"),
    "Phoenix":        (33.4484, -112.0740, "US"),
    "Philadelphia":   (39.9526, -75.1652, "US"),
    "San Diego":      (32.7157, -117.1611, "US"),
    "Las Vegas":      (36.1699, -115.1398, "US"),
    "Nashville":      (36.1627, -86.7816, "US"),
    "Minneapolis":    (44.9778, -93.2650, "US"),
    "New Orleans":    (29.9511, -90.0715, "US"),
    "Washington DC":  (38.9072, -77.0369, "US"),
    "Washington":     (38.9072, -77.0369, "US"),
    "DC":             (38.9072, -77.0369, "US"),
    "San Jose":       (37.3382, -121.8863, "US"),
    "Oakland":        (37.8044, -122.2712, "US"),
    "Pittsburgh":     (40.4406, -79.9959, "US"),
    "Charlotte":      (35.2271, -80.8431, "US"),
    "Indianapolis":   (39.7684, -86.1581, "US"),
    "Columbus":       (39.9612, -82.9988, "US"),
    "Fort Worth":     (32.7555, -97.3308, "US"),
    "Memphis":        (35.1495, -90.0490, "US"),
    "Baltimore":      (39.2904, -76.6122, "US"),
    "Louisville":     (38.2527, -85.7585, "US"),
    "Milwaukee":      (43.0389, -87.9065, "US"),
    "Albuquerque":    (35.0844, -106.6504, "US"),
    "Tucson":         (32.2226, -110.9747, "US"),
    "Fresno":         (36.7378, -119.7871, "US"),
    "Sacramento":     (38.5816, -121.4944, "US"),
    "Salt Lake City": (40.7608, -111.8910, "US"),
    "Kansas City":    (39.0997, -94.5786, "US"),
    "Long Beach":     (33.7701, -118.1937, "US"),
    "Raleigh":        (35.7796, -78.6382, "US"),
    "Tampa":          (27.9506, -82.4572, "US"),
    "Orlando":        (28.5383, -81.3792, "US"),
    "Cincinnati":     (39.1031, -84.5120, "US"),
    "Cleveland":      (41.4993, -81.6944, "US"),
    "St. Louis":      (38.6270, -90.1994, "US"),
    "Detroit":        (42.3314, -83.0458, "US"),
}


def _build_queries(
    intent: VenueIntent,
    user_area: str = "",
    user_lat: float | None = None,
    user_lng: float | None = None,
) -> list[str]:
    """
    Build up to 8 complementary search queries for maximum local coverage.

    is_gps is True whenever GPS coordinates are available — regardless of whether
    reverse-geocoding succeeded.  When is_gps=True and user_area is empty (geocode
    failed), queries use "near me" so they are location-neutral and the caller's
    locationBias circle (lat/lng + radius) does all the geospatial anchoring.
    This prevents the LLM's default city ("New York City") from appearing in query
    text and dominating text-search, which would return wrong-city results that are
    then wiped by the radius filter.
    """
    cuisine = intent.cuisine or ""
    city = intent.city
    occasion = intent.occasion.replace("_", " ").lower()
    signals = [s.lower() for s in (intent.other_signals or [])]
    all_terms = {occasion} | set(signals) | ({cuisine.lower()} if cuisine else set())

    # ── Location string resolution ─────────────────────────────────────────
    # is_gps: True whenever coordinates are known, not just when geocode succeeded.
    is_gps = user_lat is not None and user_lng is not None
    if user_area:
        parts = [p.strip() for p in user_area.split(",")]
        # "Maplewood, Essex County, NJ" → primary = "Maplewood NJ", broad = "Essex County NJ"
        primary_loc = f"{parts[0]} {parts[-1]}" if len(parts) >= 2 else parts[0]
        broad_loc   = f"{parts[1]} {parts[-1]}" if len(parts) >= 3 else primary_loc
        location    = primary_loc
    elif is_gps:
        # Reverse-geocode failed — use neutral text; locationBias handles anchoring.
        location  = "near me"
        broad_loc = "near me"
    elif city == "Unknown":
        location  = next((s for s in (intent.other_signals or []) if len(s) > 3), "near me")
        broad_loc = location
    elif intent.neighborhood:
        location  = f"{intent.neighborhood} {city}"
        broad_loc = location
    else:
        location  = city
        broad_loc = city

    # ── Office / corporate HQ ─────────────────────────────────────────────
    is_office_search = (
        occasion in _OFFICE_OCCASIONS
        or any(kw in signals for kw in ("office", "headquarters", "hq", "corporate", "company"))
    )
    if is_office_search:
        named = [s for s in signals if s not in ("office", "offices", "headquarters", "hq", "corporate", "company", "near")]
        if named:
            co = " ".join(named[:2])
            base = [
                f"{co} headquarters office {location}",
                f"corporate headquarters office buildings {location}",
                f"tech company offices business district {location}",
            ]
        else:
            base = [
                f"corporate headquarters major company offices {location}",
                f"tech company office buildings {location}",
                f"business district office towers {location}",
            ]
        return (base + [f"company offices {broad_loc}", "office building"])[:8] if is_gps else base

    # ── Outdoor / hiking / nature ─────────────────────────────────────────
    is_outdoor_search = any(kw in all_terms for kw in _OUTDOOR_KEYWORDS)
    if is_outdoor_search:
        activity = next((kw for kw in ("hiking", "trail", "walking", "cycling", "trekking") if kw in all_terms), "hiking trail")
        if is_gps:
            return [
                f"{activity} trails parks near {location}",
                f"best {activity} trails near {location}",
                f"nature parks scenic trails {location}",
                f"{activity} trails {broad_loc}",
                f"parks nature trails near me",
                f"scenic {activity} route",
            ]
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
        return [
            f"{activity} trails parks near {location}",
            f"best {activity} trails {region}",
            f"nature parks scenic trails day trips near {location}",
        ]

    # ── Café / remote-work / wifi ─────────────────────────────────────────
    has_wifi = any(kw in all_terms for kw in _WIFI_KEYWORDS)
    is_cafe_search = (
        any(kw in all_terms for kw in _CAFE_KEYWORDS)
        or cuisine.lower() in ("cafe", "café", "coffee", "coffee shop")
        or has_wifi
    )
    if is_cafe_search:
        wifi_tag = " with wifi" if has_wifi else ""
        base = [
            f"café coffee shop{wifi_tag} laptop friendly {location}",
            f"best café to work from{wifi_tag} {location}",
            f"quiet coffee shop{wifi_tag} {location}",
        ]
        if is_gps:
            base += [
                f"café coffee shop{wifi_tag} {broad_loc}",
                f"local coffee shop{wifi_tag} near me",
                f"coffee shop{wifi_tag}",
            ]
        return base[:8]

    # ── Non-restaurant public venue types ─────────────────────────────────
    _PUBLIC_VENUES = {
        "library", "libraries", "museum", "museums", "gallery", "galleries",
        "gym", "fitness", "pool", "swimming", "bowling", "cinema", "theatre",
        "theater", "arcade", "bookstore", "bookshop", "market", "farmers market",
        "spa", "salon", "pharmacy", "clinic", "hospital", "bank", "post office",
    }

    has_museum  = any(t in all_terms for t in ("museum", "museums"))
    has_gallery = any(t in all_terms for t in ("gallery", "galleries"))
    if has_museum or has_gallery:
        if has_museum and has_gallery:
            base = [
                f"museums and galleries {location}",
                f"art museum natural history museum {location}",
                f"science museum children's museum {location}",
                f"best cultural institutions museums {location}",
                f"contemporary art gallery exhibition {location}",
            ]
        elif has_museum:
            base = [
                f"museums {location}",
                f"art museum natural history museum {location}",
                f"science museum technology museum {location}",
                f"children's museum history museum {location}",
                f"best museums cultural institutions {location}",
            ]
        else:
            base = [
                f"art gallery {location}",
                f"best art galleries {location}",
                f"contemporary art gallery exhibition {location}",
                f"galleries museums {location}",
                f"photography gallery design gallery {location}",
            ]
        if is_gps:
            base += [f"museum gallery {broad_loc}", "museum gallery near me"]
        return base[:8]

    venue_type = cuisine or ""
    if not venue_type:
        venue_type = next((t for t in _PUBLIC_VENUES if t in all_terms), "")
    if not venue_type:
        venue_type = occasion if any(t in occasion for t in _PUBLIC_VENUES) else "restaurant"

    # Only use the non-restaurant path when no cuisine is set.  If cuisine is
    # specified (even a specific dish like "pancakes" or "tacos"), the restaurant
    # query branch below produces better Google Places results.
    if not cuisine and venue_type != "restaurant" and venue_type not in {"dining", "dinner", "lunch", "brunch", "breakfast"}:
        base = [
            f"{venue_type} {location}",
            f"best {venue_type} near {location}",
            f"{occasion} {venue_type} {location}" if occasion != venue_type else f"top {venue_type} {location}",
        ]
        if is_gps:
            base += [f"{venue_type} {broad_loc}", venue_type]
        return base[:8]

    # ── Restaurants — 8 queries for GPS, 5 for named-city ────────────────
    cuisine_tag = f" {cuisine}" if cuisine else ""
    # For specific food items, look up a broader category to widen coverage.
    # "pancakes" → "breakfast", "tacos" → "mexican", "sushi" → "japanese", etc.
    # This catches venues that serve the dish but don't use the dish name in their listing.
    food_cat = _FOOD_TO_CATEGORY.get(cuisine.lower(), "") if cuisine else ""
    # Only use category tag when it differs from the cuisine (avoid "pizza pizza restaurant")
    cat_query_3 = (
        f"best {food_cat} restaurant {location}" if food_cat and food_cat != cuisine.lower()
        else f"popular{cuisine_tag} dining {location}"
    )
    if is_gps:
        # `category` = the cuisine ("sushi") or "restaurant" when none given.
        # `cat_with_rest` adds "restaurant" after a specific cuisine so queries always
        # contain a dining-category word — prevents "best North Caldwell NJ" from
        # returning nail salons, gyms, etc. alongside actual restaurants.
        category = cuisine if cuisine else "restaurant"
        cat_with_rest = f"{cuisine} restaurant" if cuisine else "restaurant"

        # For directional place names (North/South/East/West + base name), build an
        # area-cluster query by stripping the prefix.  "North Caldwell NJ" →
        # "Caldwell area NJ" so Google finds Caldwell, West Caldwell, Fairfield, etc.
        # For non-directional names (Trenton, Brooklyn) the area_loc stays the same.
        _DIR_PREFIXES = ("north ", "south ", "east ", "west ")
        area_loc = location
        if location.lower() != "near me":
            parts_loc = location.rsplit(" ", 1)
            if len(parts_loc) == 2 and parts_loc[1].isupper() and len(parts_loc[1]) == 2:
                base_name, state = parts_loc
                for pfx in _DIR_PREFIXES:
                    if base_name.lower().startswith(pfx):
                        stripped = base_name[len(pfx):]
                        area_loc = f"{stripped} area {state}"
                        break

        # Three location cases:
        # 1. has_broad: neighborhood + county both known → use near-location + area + county
        # 2. local-only: city/state known but no county → use near-location + area queries
        # 3. near-me: reverse-geocode failed → pure proximity queries with phrase variety
        has_broad = broad_loc != location and location != "near me"
        if has_broad:
            return [
                f"best {cat_with_rest} {location}",
                f"best {cat_with_rest} near {location}",
                cat_query_3,
                f"top rated {cat_with_rest} near {location}",
                f"best {cat_with_rest} {area_loc}" if area_loc != location else f"best {cat_with_rest} {broad_loc}",
                f"popular {cat_with_rest} near {location}",
                f"local {category} near me",
                f"{category} near me",
            ]
        if location != "near me":
            # local-only: good city name but no county fallback — rely on near-location queries
            return [
                f"best {cat_with_rest} {location}",
                f"best {cat_with_rest} near {location}",
                cat_query_3,
                f"top rated {cat_with_rest} near {location}",
                f"best {cat_with_rest} {area_loc}" if area_loc != location else f"popular {cat_with_rest} near {location}",
                f"highly rated {cat_with_rest} {location}",
                f"local {category} near me",
                f"{category} near me",
            ]
        # near-me: vary phrasing so Google returns diverse results across ranking signals
        return [
            f"best {cat_with_rest} near me",
            f"top rated {cat_with_rest} near me",
            cat_query_3,
            f"highly rated {cat_with_rest} near me",
            f"popular {category} near me",
            f"local {category} near me",
            f"good {cat_with_rest} near me",
            f"{category} near me",
        ]
    return [
        f"best{cuisine_tag} restaurant {location}",
        f"{cuisine_tag} restaurant {location}".strip(),
        cat_query_3,
        f"top rated{cuisine_tag} restaurant {location}",
        f"popular{cuisine_tag} restaurant {location}",
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
        user_area: str = "",
        city_lat: float | None = None,
        city_lng: float | None = None,
    ) -> list[dict]:
        queries = _build_queries(intent, user_area=user_area, user_lat=user_lat, user_lng=user_lng)
        # Nimble location: derive "Maplewood New Jersey" style from user_area when GPS is set.
        # When GPS is active but reverse-geocode failed (user_area=""), pass "" rather than
        # intent.city — intent.city defaults to "New York City" and would bias Nimble to NYC
        # even for users in Trenton or North Caldwell. locationBias handles geospatial anchoring.
        if user_area:
            area_parts = [p.strip() for p in user_area.split(",")]
            nimble_location = f"{area_parts[0]} {area_parts[-1]}" if len(area_parts) >= 2 else area_parts[0]
        elif user_lat is not None and user_lng is not None:
            nimble_location = ""
        else:
            nimble_location = intent.neighborhood or intent.city
        all_signals_lower = " ".join([intent.occasion] + (intent.other_signals or [])).lower()
        open_now = any(kw in all_signals_lower for kw in _OPEN_NOW_KEYWORDS)
        is_outdoor = any(kw in all_signals_lower for kw in _OUTDOOR_KEYWORDS)

        # Build location restriction — always required to prevent cross-country results.
        # GPS coordinates take priority; then hardcoded city coords; then live geocoding.
        # Hardcoded coords ensure a geocoding failure never silently disables restriction.
        country_code = ""
        if user_lat is not None and user_lng is not None:
            # When the frontend passes a tight radius (e.g. 2000m for "near me"),
            # honour it so results stay within the user's neighborhood.
            # Default 5000m (city block scale) instead of 15000m so GPS searches
            # don't silently expand to cover the whole city.
            radius = max(500.0, min(50000.0, user_radius_m or 5000.0))
            bias: dict | None = {"lat": user_lat, "lng": user_lng, "radius_m": radius}
            country_code = "US"
        elif intent.city not in ("Unknown", ""):
            bias = None
            city_key = intent.city.strip()
            if city_key in _CITY_COORDS:
                city_radius = 80000.0 if is_outdoor else 30000.0
                clat, clng, country_code = _CITY_COORDS[city_key]
                bias = {"lat": clat, "lng": clng, "radius_m": city_radius}
            elif city_lat is not None and city_lng is not None:
                # Use the pre-geocoded coordinates from the orchestrator — avoids a
                # second independent geocoding call that could return different results.
                city_radius = 80000.0 if is_outdoor else 20000.0
                bias = {"lat": city_lat, "lng": city_lng, "radius_m": city_radius}
            else:
                # Last-resort: geocode independently (orchestrator geocode must have failed).
                # Validate that the result actually maps to the queried city — geocoding
                # "North Caldwell" can return "N. Caldwell St, Charlotte, NC" which would
                # bias the search to the wrong state.  If validation fails, leave bias=None
                # so Google Places uses only the text query for location inference.
                city_radius = 80000.0 if is_outdoor else 20000.0
                try:
                    async with GoogleMapsClient() as geocoder:
                        geo = await geocoder.geocode(intent.city)
                        city_words = intent.city.lower().split()
                        if not geo or not all(w in geo.formatted_address.lower() for w in city_words):
                            # Retry with USA qualifier for suburbs that geocode to wrong places
                            geo = await geocoder.geocode(f"{intent.city}, USA")
                        if geo and all(w in geo.formatted_address.lower() for w in city_words):
                            bias = {"lat": geo.latitude, "lng": geo.longitude, "radius_m": city_radius}
                except Exception:
                    pass
        else:
            bias = None

        # ── Phase 1: Google Places (must complete) + Nimble (best-effort, 10s cap) ──
        async with GoogleMapsClient() as maps:
            google_tasks = [maps.search_venues(q, max_results=20, location_bias=bias, open_now=open_now) for q in queries]
            google_batches = await asyncio.gather(*google_tasks, return_exceptions=True)

        nimble_maps_batches: list[Any] = []
        nimble_serp_batches: list[Any] = []
        try:
            async with NimbleClient() as nimble:
                nimble_tasks = [
                    nimble.maps_search(queries[0], nimble_location, country=country_code),
                    *[nimble.serp_search(q, country=country_code) for q in queries[:2]],
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

        # Google Places returns transit stops, bus stops, and subway stations
        # alongside real venues. These type strings appear in the `types` list
        # returned by the Places API and should never reach the scoring engine.
        _TRANSIT_TYPES = frozenset({
            "transit_station", "subway_station", "train_station", "bus_station",
            "light_rail_station", "ferry_terminal", "airport", "bus_stop",
        })

        # Non-dining commercial/service types that pollute restaurant searches.
        # Drop any venue whose types are exclusively from this set AND contain
        # none of the dining types below.
        _NON_DINING_TYPES = frozenset({
            # Retail
            "liquor_store", "grocery_or_supermarket", "supermarket",
            "convenience_store", "hardware_store", "home_goods_store",
            "furniture_store", "electronics_store", "clothing_store",
            "shoe_store", "jewelry_store", "pharmacy", "flooring_store",
            "appliance_store", "pet_store", "bicycle_store", "book_store",
            "car_dealer", "department_store", "shopping_mall",
            # Auto
            "gas_station", "car_wash", "car_repair", "parking",
            # Beauty / personal care — nail salons, spas, hair salons
            "beauty_salon", "nail_salon", "hair_care", "spa",
            "barber_shop", "hair_salon",
            # Health / medical
            "doctor", "dentist", "hospital", "veterinary_care",
            "physiotherapist", "health",
            # Finance / legal / real estate
            "bank", "atm", "insurance_agency", "real_estate_agency",
            "lawyer", "accounting",
            # Contractors / tradespeople
            "locksmith", "plumber", "electrician",
            "roofing_contractor", "general_contractor", "painter",
            "moving_company",
            # Other services
            "laundry", "storage",
        })
        _DINING_TYPES = frozenset({
            "restaurant", "bar", "cafe", "food", "meal_takeaway",
            "meal_delivery", "bakery", "night_club", "coffee_shop",
        })

        # Non-dining filter only makes sense for food/restaurant searches.
        # "best nail salon" or "gym near me" must NOT have their results filtered out.
        _DINING_OCCASION_FRAGMENTS = (
            "din", "lunch", "brunch", "breakfast", "cafe", "bar",
            "restaurant", "happy", "cocktail", "drink", "coffee",
            "date", "birthday", "food",
        )
        is_food_search = bool(intent.cuisine) or any(
            frag in intent.occasion.lower() for frag in _DINING_OCCASION_FRAGMENTS
        )

        def _ingest(batch: Any, source_fallback: str) -> None:
            if isinstance(batch, Exception) or not isinstance(batch, list):
                return
            for v in batch:
                if not v.get("name", "").strip():
                    continue  # never ingest nameless venues — they produce blank cards
                place_types = set(v.get("types", []))
                # Drop transit infrastructure — Google Places returns subway stations
                # adjacent to real venues (e.g. "81 St-Museum of Natural History" subway stop)
                if place_types & _TRANSIT_TYPES:
                    continue
                # Drop non-dining businesses (nail salons, contractors, banks, etc.)
                # ONLY for food/restaurant searches — if the user explicitly searches
                # "best nail salons" or "gym near me", let those results through.
                if is_food_search and place_types & _NON_DINING_TYPES and not place_types & _DINING_TYPES:
                    continue
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
                    google_rating=v.get("rating"),
                ))

        for batch in google_batches:
            _ingest(batch, "google_places")
        for batch in nimble_maps_batches:
            _ingest(batch, "nimble_maps")

        # Drop venues whose coordinates fall outside the restriction radius.
        # Nimble results bypass locationRestriction, so this is the safety net.
        if bias:
            clat, clng, max_m = bias["lat"], bias["lng"], bias["radius_m"]
            def _in_radius(v: RawVenueResult) -> bool:
                if v.latitude is None or v.longitude is None:
                    return True  # no coords — keep for list; orchestrator filter handles the rest
                dlat = (v.latitude - clat) * 111320
                dlng = (v.longitude - clng) * 111320 * math.cos(math.radians(clat))
                return math.sqrt(dlat ** 2 + dlng ** 2) <= max_m * 1.15  # 15% buffer
            raw_venues = [v for v in raw_venues if _in_radius(v)]

        # ── Phase 2: Claude signal extraction (top 25 only for speed) ──────────
        # Remaining venues are included as base data so the scorer can still
        # rank them — they just won't have noise/occasion/capacity signals.
        all_venues = raw_venues[:_MAX_VENUES]
        extraction_batch = all_venues[:_CLAUDE_EXTRACTION_LIMIT]
        base_only = all_venues[_CLAUDE_EXTRACTION_LIMIT:]

        signals_list = await asyncio.gather(
            *[_call_with_retry(v) for v in extraction_batch], return_exceptions=True
        )

        # Price hint from Google Places API (most reliable source)
        places_price: dict[str, int] = {}
        for batch in google_batches:
            if isinstance(batch, list):
                for v in batch:
                    if v.get("place_id") and v.get("price_per_head_usd"):
                        places_price[v["place_id"]] = v["price_per_head_usd"]

        enriched: list[dict] = []
        for raw_venue, signals in zip(extraction_batch, signals_list):
            base = raw_venue.model_dump()
            if isinstance(signals, Exception) or signals is None:
                enriched.append(base)
                continue
            sig_dict = signals.model_dump()
            if not sig_dict.get("price_per_head_usd") and raw_venue.place_id in places_price:
                sig_dict["price_per_head_usd"] = places_price[raw_venue.place_id]
            ev = EnrichedVenue(**{**base, **sig_dict})
            enriched.append(ev.model_dump())

        # Append remaining raw venues (no Claude signals — scorer uses base score)
        for raw_venue in base_only:
            enriched.append(raw_venue.model_dump())

        return enriched
