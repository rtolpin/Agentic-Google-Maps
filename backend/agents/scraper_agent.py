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


_OFFICE_OCCASIONS = {
    "offices", "office", "scouting offices", "corporate", "business",
    "scout offices", "office scouting", "office space", "company offices",
    "corporate offices", "headquarters",
}
# Industry sectors that modify office queries (e.g. "tech offices" → "tech company offices")
_OFFICE_SECTORS = {
    "tech": "tech company",
    "technology": "tech company",
    "finance": "financial",
    "financial": "financial",
    "startup": "startup",
    "startups": "startup",
    "law": "law firm",
    "legal": "law firm",
    "media": "media company",
    "fashion": "fashion company",
    "advertising": "advertising agency",
    "consulting": "consulting firm",
    "healthcare": "healthcare",
    "biotech": "biotech",
    "pharma": "pharmaceutical",
}

_CAFE_KEYWORDS = {"cafe", "café", "coffee", "cosy", "cozy", "laptop", "remote", "work from", "working"}
_WIFI_KEYWORDS = {"wifi", "wi-fi", "internet", "laptop"}

_OUTDOOR_KEYWORDS = {
    "hiking", "hike", "trail", "trails", "nature", "park", "parks", "outdoor", "outdoors",
    "walk", "walking", "trekking", "trek", "forest", "mountain", "mountains",
    "waterfall", "scenic", "wilderness", "campsite", "camping", "cycling", "bike trail",
    "greenway", "preserve", "state park", "national park",
    "garden", "gardens", "botanical", "arboretum", "meadow", "green space",
}
# Park/garden keywords separate from active-trail keywords — drive different query templates.
_PARK_KEYWORDS = {"park", "parks", "garden", "gardens", "botanical", "arboretum", "meadow", "green space", "relax", "peaceful", "picnic"}

_SHOPPING_KEYWORDS = {
    "mall", "shopping mall", "shopping center", "shop", "shops", "shopping",
    "store", "stores", "boutique", "boutiques", "retail",
    "clothing", "clothes", "fashion", "apparel", "outfit", "outfits",
    "bridal", "bride", "bridesmaid", "wedding dress", "wedding gown",
    "tuxedo", "tux", "suit", "suits", "formal wear", "menswear", "men's wear",
    "department store", "buy", "purchase",
    "rei", "target", "macy", "bloomingdale", "nordstrom", "saks", "neiman",
    "zara", "h&m", "gap", "banana republic", "j.crew", "uniqlo",
    "anthropologie", "free people", "lululemon", "nike", "adidas",
}
_BRIDAL_KEYWORDS   = {"bridal", "bride", "bridesmaid", "wedding dress", "wedding gown", "bridal gown"}
_FORMAL_KEYWORDS   = {"tuxedo", "tux", "suit", "suits", "formal wear", "menswear", "men's wear", "black tie", "dress shirt"}
_MALL_KEYWORDS     = {"mall", "shopping mall", "shopping center"}
# Named retailers → canonical search label
_NAMED_RETAILERS: dict[str, str] = {
    "rei":           "REI outdoor gear store",
    "target":        "Target store",
    "macy":          "Macy's department store",
    "bloomingdale":  "Bloomingdale's department store",
    "nordstrom":     "Nordstrom department store",
    "saks":          "Saks Fifth Avenue",
    "neiman":        "Neiman Marcus",
    "zara":          "Zara clothing store",
    "h&m":           "H&M clothing store",
    "gap":           "Gap clothing store",
    "banana republic": "Banana Republic",
    "j.crew":        "J.Crew",
    "uniqlo":        "Uniqlo",
    "anthropologie": "Anthropologie",
    "free people":   "Free People store",
    "lululemon":     "Lululemon",
    "nike":          "Nike store",
    "adidas":        "Adidas store",
}

# Named companies → canonical name used in office search queries.
# Key is a lowercase match token; value is the display name for the query.
_NAMED_COMPANIES: dict[str, str] = {
    # Finance / banking
    "jp morgan": "JPMorgan Chase",
    "jpmorgan": "JPMorgan Chase",
    "goldman sachs": "Goldman Sachs",
    "goldman": "Goldman Sachs",
    "morgan stanley": "Morgan Stanley",
    "citibank": "Citibank",
    "citi": "Citibank",
    "bank of america": "Bank of America",
    "wells fargo": "Wells Fargo",
    "blackrock": "BlackRock",
    "bloomberg": "Bloomberg",
    "capital one": "Capital One",
    "american express": "American Express",
    "amex": "American Express",
    "fidelity": "Fidelity Investments",
    "charles schwab": "Charles Schwab",
    "barclays": "Barclays",
    "hsbc": "HSBC",
    "deutsche bank": "Deutsche Bank",
    "ubs": "UBS",
    "credit suisse": "Credit Suisse",
    "two sigma": "Two Sigma",
    "citadel": "Citadel",
    "bridgewater": "Bridgewater Associates",
    # Big tech
    "google": "Google",
    "alphabet": "Google",
    "apple": "Apple",
    "meta": "Meta",
    "facebook": "Meta",
    "amazon": "Amazon",
    "microsoft": "Microsoft",
    "netflix": "Netflix",
    "salesforce": "Salesforce",
    "stripe": "Stripe",
    "airbnb": "Airbnb",
    "uber": "Uber",
    "lyft": "Lyft",
    "spotify": "Spotify",
    "twitter": "X (Twitter)",
    "linkedin": "LinkedIn",
    "oracle": "Oracle",
    "ibm": "IBM",
    "cisco": "Cisco",
    "intel": "Intel",
    "nvidia": "NVIDIA",
    "datadog": "Datadog",
    "snowflake": "Snowflake",
    "palantir": "Palantir",
    "databricks": "Databricks",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "coinbase": "Coinbase",
    "robinhood": "Robinhood",
    "paypal": "PayPal",
    "venmo": "Venmo",
    "square": "Block (Square)",
    "block": "Block (Square)",
    "visa": "Visa",
    "mastercard": "Mastercard",
    # Consulting / professional services
    "mckinsey": "McKinsey & Company",
    "bcg": "Boston Consulting Group",
    "bain": "Bain & Company",
    "deloitte": "Deloitte",
    "kpmg": "KPMG",
    "pwc": "PwC",
    "ernst & young": "EY",
    "accenture": "Accenture",
    "booz allen": "Booz Allen Hamilton",
    # Media / entertainment
    "nbcuniversal": "NBCUniversal",
    "nbc": "NBCUniversal",
    "disney": "Disney",
    "viacom": "Paramount",
    "paramount": "Paramount",
    "conde nast": "Condé Nast",
    "hearst": "Hearst",
    "new york times": "New York Times",
    "nyt": "New York Times",
    "warner": "Warner Bros Discovery",
    "wpp": "WPP",
    # Healthcare / pharma
    "johnson & johnson": "Johnson & Johnson",
    "pfizer": "Pfizer",
    "merck": "Merck",
    "bristol myers": "Bristol Myers Squibb",
    "abbvie": "AbbVie",
    # Other
    "spacex": "SpaceX",
    "tesla": "Tesla",
    "general electric": "GE",
    "boeing": "Boeing",
    "lockheed": "Lockheed Martin",
}

# Occasion-specific search modifiers — inject into restaurant queries so Google
# Places returns romantically-appropriate venues instead of generic results.
_ROMANTIC_SIGNALS = {
    "romantic", "romance", "anniversary", "date night", "date", "intimate",
    "candle", "candlelit", "special night", "couples", "honeymoon", "proposal",
}
_BIRTHDAY_SIGNALS = {"birthday", "celebrate", "celebration", "bday"}
_BUSINESS_SIGNALS = {"business", "work lunch", "corporate lunch", "client dinner", "business dinner"}
_GROUP_SIGNALS = {"group", "party", "birthday party", "large group", "team dinner"}

def _occasion_query_tag(occasion: str, signals: list[str]) -> str:
    """
    Return a search-query prefix that narrows Google Places to the right venue type.
    Returns empty string when the occasion is generic (plain dinner, lunch, etc.).
    """
    combined = f"{occasion} {' '.join(signals)}".lower()
    if any(kw in combined for kw in _ROMANTIC_SIGNALS):
        return "romantic fine dining"
    if "dinner for two" in combined or "table for two" in combined:
        return "romantic dinner"
    if any(kw in combined for kw in _BIRTHDAY_SIGNALS):
        return "birthday dinner celebration"
    if any(kw in combined for kw in _BUSINESS_SIGNALS):
        return "business dining"
    if any(kw in combined for kw in _GROUP_SIGNALS):
        return "group dining"
    return ""

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
    "New York City":  (40.7549, -73.9840, "US"),  # Midtown — central for restaurant searches
    "New York":       (40.7549, -73.9840, "US"),
    "NYC":            (40.7549, -73.9840, "US"),
    "Manhattan":      (40.7549, -73.9840, "US"),
    # NYC neighborhoods — used for tight-radius bias when intent.neighborhood is set
    "Midtown":              (40.7549, -73.9840, "US"),
    "Midtown Manhattan":    (40.7549, -73.9840, "US"),
    "Midtown East":         (40.7549, -73.9745, "US"),
    "Midtown West":         (40.7580, -73.9855, "US"),
    "Hell's Kitchen":       (40.7638, -73.9918, "US"),
    "Chelsea":              (40.7465, -74.0014, "US"),
    "Flatiron":             (40.7410, -73.9897, "US"),
    "Gramercy":             (40.7379, -73.9840, "US"),
    "Greenwich Village":    (40.7336, -74.0027, "US"),
    "West Village":         (40.7358, -74.0036, "US"),
    "East Village":         (40.7265, -73.9857, "US"),
    "SoHo":                 (40.7233, -74.0030, "US"),
    "Tribeca":              (40.7163, -74.0086, "US"),
    "Lower Manhattan":      (40.7074, -74.0113, "US"),
    "Financial District":   (40.7074, -74.0113, "US"),
    "Upper East Side":      (40.7736, -73.9566, "US"),
    "Upper West Side":      (40.7870, -73.9754, "US"),
    "Harlem":               (40.8116, -73.9465, "US"),
    "Astoria":              (40.7721, -73.9302, "US"),
    "Long Island City":     (40.7448, -73.9483, "US"),
    "Williamsburg":         (40.7081, -73.9571, "US"),
    "Bushwick":             (40.6942, -73.9213, "US"),
    "Park Slope":           (40.6710, -73.9814, "US"),
    "DUMBO":                (40.7033, -73.9893, "US"),
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


def _gps_overridden_by_intent(intent: VenueIntent, user_area: str) -> bool:
    """
    True when the query explicitly names a neighborhood or a city that differs
    from the GPS-derived area.  In that case GPS coordinates should NOT drive
    query text or locationBias — the user is asking about a specific place.

    Examples:
      user in Trenton, query "best restaurant Upper East Side" → True
      user in Hell's Kitchen, query "best restaurant Upper East Side" → True
      user in NYC, query "best restaurant near me" → False
    """
    # Explicit neighborhood always means "search here, not where I am"
    nbhd = (intent.neighborhood or "").strip()
    if nbhd and nbhd.lower() not in ("near me", "nearby"):
        return True
    # City-level: check if intent city appears in the GPS area
    city = (intent.city or "").strip()
    if city in ("Unknown", "") or not user_area:
        return False
    city_words = {w.lower() for w in city.replace(",", "").split() if len(w) > 2}
    area_words = {w.lower() for w in user_area.replace(",", "").split() if len(w) > 2}
    return city_words.isdisjoint(area_words)


def _build_queries(
    intent: VenueIntent,
    user_area: str = "",
    user_lat: float | None = None,
    user_lng: float | None = None,
) -> list[str]:
    """
    Build up to 8 complementary search queries for maximum local coverage.

    is_gps is True whenever coordinates are available.  When the query explicitly
    names a neighborhood or different city (_gps_overridden_by_intent), GPS is
    ignored so "best restaurant Upper East Side" from Trenton searches UES, not Trenton.
    """
    cuisine = intent.cuisine or ""
    city = intent.city
    occasion = intent.occasion.replace("_", " ").lower()
    signals = [s.lower() for s in (intent.other_signals or [])]
    all_terms = {occasion} | set(signals) | ({cuisine.lower()} if cuisine else set())
    all_terms_str = " ".join(all_terms)  # substring-safe join for keyword detection

    # ── Location string resolution ─────────────────────────────────────────
    is_gps = user_lat is not None and user_lng is not None
    # When the intent names a specific place, ignore GPS and use the intent location.
    use_gps = is_gps and not _gps_overridden_by_intent(intent, user_area)

    if user_area and use_gps:
        parts = [p.strip() for p in user_area.split(",")]
        # "Maplewood, Essex County, NJ" → primary = "Maplewood NJ", broad = "Essex County NJ"
        primary_loc = f"{parts[0]} {parts[-1]}" if len(parts) >= 2 else parts[0]
        broad_loc   = f"{parts[1]} {parts[-1]}" if len(parts) >= 3 else primary_loc
        location    = primary_loc
    elif use_gps:
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
        or any(kw in all_terms_str for kw in ("office", "headquarters", "hq", "corporate", "coworking", "co-working", "workspace"))
    )
    if is_office_search:
        _OFFICE_STOP = {
            "office", "offices", "headquarters", "hq", "corporate", "company",
            "companies", "near", "scout", "scouting", "building", "buildings",
            "space", "spaces", "business", "work", "working",
        }
        # Named company takes highest priority (e.g. "JP Morgan offices" → JPMorgan Chase)
        named_co = next(
            (label for kw, label in _NAMED_COMPANIES.items() if kw in all_terms_str),
            None,
        )
        # Detect industry sector (e.g. "tech offices" → sector = "tech company")
        sector = next(
            (label for kw, label in _OFFICE_SECTORS.items() if kw in all_terms_str),
            None,
        )
        named = [s for s in signals if s not in _OFFICE_STOP and len(s) > 2]
        if named_co:
            base = [
                f"{named_co} office {location}",
                f"{named_co} headquarters {location}",
                f"{named_co} office building {location}",
                f"{named_co}",
            ]
        elif sector:
            base = [
                f"{sector} offices {location}",
                f"{sector} office buildings {location}",
                f"{sector} company offices {location}",
                f"corporate office {location}",
                f"office buildings business district {location}",
                f"coworking space {location}",
                f"office park {location}",
            ]
        elif named:
            co = " ".join(named[:2])
            base = [
                f"{co} office {location}",
                f"{co} headquarters {location}",
                f"corporate office buildings {location}",
                f"company offices business district {location}",
                f"office building {location}",
                f"commercial office {location}",
            ]
        else:
            base = [
                f"corporate office {location}",
                f"company offices {location}",
                f"office buildings {location}",
                f"business district office {location}",
                f"commercial office space {location}",
                f"coworking space {location}",
                f"WeWork offices {location}",
                f"office park {location}",
            ]
        if use_gps:
            base += [f"office buildings near me", "coworking office space"]
        return base[:8]

    # ── Outdoor / parks / hiking / nature ────────────────────────────────
    is_outdoor_search = any(kw in all_terms_str for kw in _OUTDOOR_KEYWORDS)
    if is_outdoor_search:
        is_park_search = any(kw in all_terms_str for kw in _PARK_KEYWORDS)
        is_trail_search = any(kw in all_terms_str for kw in ("hiking", "hike", "trail", "trek", "cycling"))

        if is_park_search and not is_trail_search:
            # User wants parks/gardens to visit/relax in, not hiking trails
            if use_gps:
                return [
                    f"public park near {location}",
                    f"city park {location}",
                    f"botanical garden near {location}",
                    f"peaceful park garden {location}",
                    f"nature park {broad_loc}",
                    f"green space park near me",
                    f"park garden",
                    f"public park",
                ]
            return [
                f"public park {location}",
                f"city park {location}",
                f"botanical garden {location}",
                f"peaceful park near {location}",
                f"nature park {location}",
            ]

        activity = next((kw for kw in ("hiking", "trail", "walking", "cycling", "trekking") if kw in all_terms_str), "hiking trail")
        if use_gps:
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

    # ── Shopping — malls, clothing, bridal, suits, named retailers ───────
    is_shopping_search = not cuisine and any(kw in all_terms_str for kw in _SHOPPING_KEYWORDS)
    if is_shopping_search:
        is_bridal  = any(kw in all_terms_str for kw in _BRIDAL_KEYWORDS)
        is_formal  = any(kw in all_terms_str for kw in _FORMAL_KEYWORDS)
        is_mall    = any(kw in all_terms_str for kw in _MALL_KEYWORDS)
        named_store = next((label for key, label in _NAMED_RETAILERS.items() if key in all_terms_str), None)

        if is_bridal:
            base = [
                f"bridal boutique {location}",
                f"wedding dress store {location}",
                f"bridesmaid dress shop near {location}",
                f"bridal shop near {location}",
                f"wedding gown boutique {location}",
                f"bridal store",
            ]
        elif is_formal:
            base = [
                f"men's suit store {location}",
                f"tuxedo rental shop {location}",
                f"formal wear store near {location}",
                f"men's clothing store {location}",
                f"suit tailor {location}",
                f"tuxedo suit store",
            ]
        elif named_store:
            base = [
                f"{named_store} {location}",
                f"{named_store} near {location}",
                named_store,
            ]
        elif is_mall:
            base = [
                f"shopping mall {location}",
                f"best shopping mall near {location}",
                f"shopping center {location}",
                f"indoor shopping mall {location}",
                f"mall stores near {location}",
            ]
        else:
            # General clothing / fashion / boutique shopping
            base = [
                f"clothing boutique {location}",
                f"fashion stores {location}",
                f"best clothing stores near {location}",
                f"women's clothing boutique {location}",
                f"shopping stores {location}",
            ]

        if use_gps:
            base += [f"shopping near me", f"stores near me"]
        return base[:8]

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

    # ── Hotels / lodging ─────────────────────────────────────────────────
    _LODGING_KWS = {
        "hotel", "hotels", "motel", "motels", "inn", "inns", "resort", "resorts",
        "lodge", "lodges", "lodging", "accommodation", "accommodations",
        "bed and breakfast", "b&b", "hostel", "hostels", "stay", "overnight stay",
    }
    is_lodging = (
        any(t in all_terms for t in _LODGING_KWS)
        or any(t in all_terms_str for t in _LODGING_KWS)
        or (cuisine.lower() in _LODGING_KWS)
    )
    if is_lodging and not cuisine.lower() in {"dining", "restaurant", "food"}:
        # Pick an occasion-specific qualifier (luxury, budget, boutique…)
        _price_tag = ""
        if intent.price_band == "luxury":
            _price_tag = "luxury 5-star "
        elif intent.price_band == "upscale":
            _price_tag = "upscale boutique "
        elif intent.price_band == "budget":
            _price_tag = "budget affordable "
        base = [
            f"{_price_tag}hotel {location}",
            f"best {_price_tag}hotels near {location}",
            f"hotel motel inn {location}",
            f"resort lodge {location}",
            f"{_price_tag}hotel accommodation {location}",
        ]
        if is_gps:
            base += [
                f"hotels near me {broad_loc}",
                f"best place to stay {location}",
                f"inn bed and breakfast {location}",
            ]
        else:
            base += [
                f"hotel resort {location}",
                f"motel inn {location}",
            ]
        return base[:8]

    # ── Non-restaurant public venue types ─────────────────────────────────
    _PUBLIC_VENUES = {
        "library", "libraries", "museum", "museums", "gallery", "galleries",
        "gym", "fitness", "pool", "swimming", "bowling", "cinema", "theatre",
        "theater", "arcade", "bookstore", "bookshop", "market", "farmers market",
        "spa", "salon", "pharmacy", "clinic", "hospital", "bank", "post office",
        "mall", "store", "stores", "boutique", "clothing", "clothes", "fashion",
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

    # Occasion-aware modifier — injects "romantic fine dining", "birthday dinner" etc.
    # into query strings so Google Places returns appropriate venue types.
    occ_tag = _occasion_query_tag(occasion, intent.other_signals or [])
    occ_prefix = f"{occ_tag} " if occ_tag else ""

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
                f"best {occ_prefix}{cat_with_rest} near {location}",
                f"best {occ_prefix}{cat_with_rest} {location}",
                f"best {occ_prefix}{cat_with_rest} {area_loc}" if area_loc != location else f"best {occ_prefix}{cat_with_rest} {broad_loc}",
                f"best {occ_prefix}{cat_with_rest} {broad_loc}",
                cat_query_3,
                f"top rated {cat_with_rest} near {location}",
                f"local {category} near me",
                f"{occ_prefix}{category} near me" if occ_prefix else f"{category} near me",
            ]
        if location != "near me":
            # local-only: good city name but no county fallback — rely on near-location queries
            return [
                f"best {occ_prefix}{cat_with_rest} near {location}",
                f"best {occ_prefix}{cat_with_rest} {location}",
                cat_query_3,
                f"top rated {cat_with_rest} near {location}",
                f"best {occ_prefix}{cat_with_rest} {area_loc}" if area_loc != location else f"popular {occ_prefix}{cat_with_rest} near {location}",
                f"highly rated {cat_with_rest} {location}",
                f"local {category} near me",
                f"{occ_prefix}{category} near me" if occ_prefix else f"{category} near me",
            ]
        # near-me: vary phrasing so Google returns diverse results across ranking signals
        return [
            f"best {occ_prefix}{cat_with_rest} near me",
            f"top rated {occ_prefix}{cat_with_rest} near me",
            cat_query_3,
            f"highly rated {cat_with_rest} near me",
            f"popular {occ_prefix}{category} near me",
            f"local {category} near me",
            f"good {cat_with_rest} near me",
            f"{occ_prefix}{category} near me" if occ_prefix else f"{category} near me",
        ]
    non_gps_cat = f"{cuisine} restaurant" if cuisine else "restaurant"
    return [
        f"best {occ_prefix}{non_gps_cat} {location}",
        f"best{cuisine_tag} restaurant {location}",
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
        gps_overridden = _gps_overridden_by_intent(intent, user_area)
        use_gps = (user_lat is not None and user_lng is not None) and not gps_overridden

        # Nimble location: use GPS area for GPS searches; use intent city when the query
        # names a specific place different from GPS location (e.g. "UES" from Trenton).
        if use_gps and user_area:
            area_parts = [p.strip() for p in user_area.split(",")]
            nimble_location = f"{area_parts[0]} {area_parts[-1]}" if len(area_parts) >= 2 else area_parts[0]
        elif use_gps:
            nimble_location = ""
        else:
            nimble_location = intent.neighborhood or intent.city

        all_signals_lower = " ".join([intent.occasion] + (intent.other_signals or [])).lower()
        open_now = any(kw in all_signals_lower for kw in _OPEN_NOW_KEYWORDS)
        # Substring match so "parks" triggers "park", "trails" triggers "trail", etc.
        is_outdoor = any(kw in all_signals_lower for kw in _OUTDOOR_KEYWORDS)

        # Build location restriction — always required to prevent cross-country results.
        # GPS coordinates take priority unless the query names a different place.
        country_code = ""
        if use_gps:
            # Outdoor/hiking searches need a much wider net — trails around suburban
            # locations like North Caldwell are typically 10-30 km away.
            if is_outdoor:
                default_radius = 25000.0
                min_radius = 15000.0  # never tighter than 15 km for trails/parks
            else:
                default_radius = 5000.0
                min_radius = 500.0
            radius = max(min_radius, min(50000.0, user_radius_m or default_radius))
            bias: dict | None = {"lat": user_lat, "lng": user_lng, "radius_m": radius}
            country_code = "US"
        elif intent.city not in ("Unknown", ""):
            bias = None
            # Neighborhood takes priority with a tight radius for precise results.
            # "best restaurant midtown" should bias to Midtown (40.75), not NYC center.
            if intent.neighborhood:
                for nbhd_key in (intent.neighborhood, f"{intent.neighborhood} Manhattan"):
                    if nbhd_key in _CITY_COORDS:
                        clat, clng, country_code = _CITY_COORDS[nbhd_key]
                        bias = {"lat": clat, "lng": clng, "radius_m": 5000.0}
                        break
            city_key = intent.city.strip()
            if bias is None and city_key in _CITY_COORDS:
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
        _LODGING_TERMS = {
            "hotel", "hotels", "motel", "motels", "inn", "inns", "resort", "resorts",
            "lodge", "lodges", "lodging", "accommodation", "accommodations",
            "bed and breakfast", "b&b", "hostel", "hostels", "stay", "overnight",
        }
        is_lodging_search = (
            (intent.cuisine or "").lower() in _LODGING_TERMS
            or any(t in intent.occasion.lower() for t in _LODGING_TERMS)
            or any(t in (s.lower() for s in (intent.other_signals or [])) for t in _LODGING_TERMS)
        )
        is_food_search = (not is_lodging_search) and (
            bool(intent.cuisine) or any(
                frag in intent.occasion.lower() for frag in _DINING_OCCASION_FRAGMENTS
            )
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
