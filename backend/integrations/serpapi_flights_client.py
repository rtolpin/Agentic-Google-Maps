"""
Serpapi Google Flights client — flight search for venues in distant cities.

Requires SERPAPI_API_KEY env var.  Results are ephemeral (displayed to user
only, never stored) and comply with Serpapi's TOS.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import httpx

SERPAPI_KEY = os.environ.get("SERPAPI_API_KEY", "")
_SERPAPI_BASE = "https://serpapi.com/search.json"

# IATA code lookup keyed by partial airport name fragment (lowercase)
_IATA_BY_NAME: dict[str, str] = {
    "heathrow": "LHR",
    "gatwick": "LGW",
    "stansted": "STN",
    "luton": "LTN",
    "london city": "LCY",
    "jfk": "JFK",
    "john f. kennedy": "JFK",
    "laguardia": "LGA",
    "la guardia": "LGA",
    "newark": "EWR",
    "los angeles international": "LAX",
    "o'hare": "ORD",
    "ohare": "ORD",
    "midway": "MDW",
    "hartsfield": "ATL",
    "dallas/fort worth": "DFW",
    "dallas fort worth": "DFW",
    "denver international": "DEN",
    "san francisco international": "SFO",
    "seattle-tacoma": "SEA",
    "seatac": "SEA",
    "miami international": "MIA",
    "charles de gaulle": "CDG",
    "orly": "ORY",
    "frankfurt": "FRA",
    "amsterdam schiphol": "AMS",
    "schiphol": "AMS",
    "dubai international": "DXB",
    "singapore changi": "SIN",
    "changi": "SIN",
    "narita": "NRT",
    "haneda": "HND",
    "beijing capital": "PEK",
    "daxing": "PKX",
    "shanghai pudong": "PVG",
    "sydney": "SYD",
    "toronto pearson": "YYZ",
    "pearson": "YYZ",
    "vancouver international": "YVR",
    "boston logan": "BOS",
    "dulles": "IAD",
    "reagan national": "DCA",
    "las vegas": "LAS",
    "phoenix sky harbor": "PHX",
    "george bush intercontinental": "IAH",
    "minneapolis-saint paul": "MSP",
    "detroit metropolitan": "DTW",
    "philadelphia international": "PHL",
    "salt lake city": "SLC",
    "portland international": "PDX",
    "san diego international": "SAN",
    "orlando international": "MCO",
    "charlotte douglas": "CLT",
    "baltimore/washington": "BWI",
    "san jose": "SJC",
    "oakland international": "OAK",
    "madrid barajas": "MAD",
    "barajas": "MAD",
    "barcelona el prat": "BCN",
    "rome fiumicino": "FCO",
    "fiumicino": "FCO",
    "milan malpensa": "MXP",
    "zurich": "ZRH",
    "vienna international": "VIE",
    "munich": "MUC",
    "brussels": "BRU",
    "lisbon": "LIS",
    "copenhagen": "CPH",
    "stockholm arlanda": "ARN",
    "oslo gardermoen": "OSL",
    "helsinki": "HEL",
    "abu dhabi": "AUH",
    "doha hamad": "DOH",
    "hong kong international": "HKG",
    "incheon": "ICN",
    "kuala lumpur": "KUL",
    "bangkok suvarnabhumi": "BKK",
    "suvarnabhumi": "BKK",
    "don mueang": "DMK",
    "delhi indira gandhi": "DEL",
    "mumbai chhatrapati": "BOM",
    "johannesburg": "JNB",
    "cape town": "CPT",
    "cairo": "CAI",
    "toronto": "YYZ",
    "chicago": "ORD",
}


def _extract_iata(airport_name: str) -> str | None:
    """Extract 3-letter IATA code from airport name string."""
    m = re.search(r'\(([A-Z]{3})\)', airport_name)
    if m:
        return m.group(1)
    name_lower = airport_name.lower()
    for pattern, code in _IATA_BY_NAME.items():
        if pattern in name_lower:
            return code
    return None


# City name (lowercase) → (IATA code, display airport name)
_CITY_AIRPORT: dict[str, tuple[str, str]] = {
    "new york city": ("JFK", "John F. Kennedy International Airport"),
    "new york":      ("JFK", "John F. Kennedy International Airport"),
    "manhattan":     ("JFK", "John F. Kennedy International Airport"),
    "brooklyn":      ("JFK", "John F. Kennedy International Airport"),
    "queens":        ("JFK", "John F. Kennedy International Airport"),
    "the bronx":     ("JFK", "John F. Kennedy International Airport"),
    "newark":        ("EWR", "Newark Liberty International Airport"),
    "hoboken":       ("EWR", "Newark Liberty International Airport"),
    "jersey city":   ("EWR", "Newark Liberty International Airport"),
    "london":        ("LHR", "London Heathrow Airport"),
    "city of london":("LHR", "London Heathrow Airport"),
    "los angeles":   ("LAX", "Los Angeles International Airport"),
    "chicago":       ("ORD", "O'Hare International Airport"),
    "san francisco": ("SFO", "San Francisco International Airport"),
    "miami":         ("MIA", "Miami International Airport"),
    "atlanta":       ("ATL", "Hartsfield-Jackson Atlanta International Airport"),
    "seattle":       ("SEA", "Seattle-Tacoma International Airport"),
    "boston":        ("BOS", "Boston Logan International Airport"),
    "dallas":        ("DFW", "Dallas/Fort Worth International Airport"),
    "houston":       ("IAH", "George Bush Intercontinental Airport"),
    "denver":        ("DEN", "Denver International Airport"),
    "las vegas":     ("LAS", "Harry Reid International Airport"),
    "phoenix":       ("PHX", "Phoenix Sky Harbor International Airport"),
    "philadelphia":  ("PHL", "Philadelphia International Airport"),
    "washington":    ("IAD", "Washington Dulles International Airport"),
    "arlington":     ("DCA", "Ronald Reagan Washington National Airport"),
    "minneapolis":   ("MSP", "Minneapolis-Saint Paul International Airport"),
    "detroit":       ("DTW", "Detroit Metropolitan Wayne County Airport"),
    "portland":      ("PDX", "Portland International Airport"),
    "san diego":     ("SAN", "San Diego International Airport"),
    "salt lake city":("SLC", "Salt Lake City International Airport"),
    "orlando":       ("MCO", "Orlando International Airport"),
    "charlotte":     ("CLT", "Charlotte Douglas International Airport"),
    "paris":         ("CDG", "Charles de Gaulle Airport"),
    "amsterdam":     ("AMS", "Amsterdam Schiphol Airport"),
    "frankfurt":     ("FRA", "Frankfurt Airport"),
    "madrid":        ("MAD", "Adolfo Suárez Madrid–Barajas Airport"),
    "barcelona":     ("BCN", "Barcelona–El Prat Airport"),
    "rome":          ("FCO", "Leonardo da Vinci–Fiumicino Airport"),
    "milan":         ("MXP", "Milan Malpensa Airport"),
    "zurich":        ("ZRH", "Zurich Airport"),
    "vienna":        ("VIE", "Vienna International Airport"),
    "munich":        ("MUC", "Munich Airport"),
    "brussels":      ("BRU", "Brussels Airport"),
    "lisbon":        ("LIS", "Lisbon Humberto Delgado Airport"),
    "copenhagen":    ("CPH", "Copenhagen Airport"),
    "stockholm":     ("ARN", "Stockholm Arlanda Airport"),
    "oslo":          ("OSL", "Oslo Gardermoen Airport"),
    "helsinki":      ("HEL", "Helsinki-Vantaa Airport"),
    "tokyo":         ("NRT", "Narita International Airport"),
    "osaka":         ("KIX", "Kansai International Airport"),
    "beijing":       ("PEK", "Beijing Capital International Airport"),
    "shanghai":      ("PVG", "Shanghai Pudong International Airport"),
    "hong kong":     ("HKG", "Hong Kong International Airport"),
    "singapore":     ("SIN", "Singapore Changi Airport"),
    "dubai":         ("DXB", "Dubai International Airport"),
    "sydney":        ("SYD", "Sydney Kingsford Smith Airport"),
    "melbourne":     ("MEL", "Melbourne Airport"),
    "toronto":       ("YYZ", "Toronto Pearson International Airport"),
    "vancouver":     ("YVR", "Vancouver International Airport"),
    "montreal":      ("YUL", "Montréal-Trudeau International Airport"),
    "seoul":         ("ICN", "Incheon International Airport"),
    "bangkok":       ("BKK", "Suvarnabhumi Airport"),
    "kuala lumpur":  ("KUL", "Kuala Lumpur International Airport"),
    "delhi":         ("DEL", "Indira Gandhi International Airport"),
    "mumbai":        ("BOM", "Chhatrapati Shivaji Maharaj International Airport"),
    "johannesburg":  ("JNB", "O.R. Tambo International Airport"),
    "cape town":     ("CPT", "Cape Town International Airport"),
    "cairo":         ("CAI", "Cairo International Airport"),
    "doha":          ("DOH", "Hamad International Airport"),
    "abu dhabi":     ("AUH", "Abu Dhabi International Airport"),
}


def _get_iata_for_area(area: dict) -> tuple[str, str] | None:
    """
    Return (iata_code, airport_name) from a reverse_geocode area dict.
    Checks city → neighborhood → county in order; partial-match fallback.
    """
    for field in ("city", "neighborhood", "county", "state"):
        val = (area.get(field) or "").lower().strip()
        if not val:
            continue
        if val in _CITY_AIRPORT:
            return _CITY_AIRPORT[val]
        for key, result in _CITY_AIRPORT.items():
            if key in val or val in key:
                return result
    return None


def _format_option(candidate: dict, departure_id: str, arrival_id: str) -> dict:
    legs = candidate.get("flights", [{}])
    first_leg = legs[0] if legs else {}
    last_leg = legs[-1] if legs else {}
    total_mins = int(candidate.get("total_duration") or 0)
    hours, mins = divmod(total_mins, 60)
    duration_str = f"{hours}h {mins}m" if hours else f"{mins}m"
    stops = max(0, len(legs) - 1)
    return {
        "price": candidate.get("price"),
        "currency": "USD",
        "duration_str": duration_str,
        "stops": stops,
        "airline": first_leg.get("airline", ""),
        "flight_number": first_leg.get("flight_number", ""),
        "departure_airport_name": first_leg.get("departure_airport", {}).get("name", departure_id),
        "departure_airport_id": first_leg.get("departure_airport", {}).get("id", departure_id),
        "arrival_airport_name": last_leg.get("arrival_airport", {}).get("name", arrival_id),
        "arrival_airport_id": last_leg.get("arrival_airport", {}).get("id", arrival_id),
    }


class SerpApiFlightsClient:
    async def search_flights(
        self,
        departure_id: str,
        arrival_id: str,
        outbound_date: str | None = None,
        max_options: int = 3,
    ) -> list[dict]:
        """
        Search for one-way flights between two IATA airport codes.
        Returns up to max_options sorted by price (cheapest first).
        Returns empty list if no flights found or key not configured.
        """
        if not SERPAPI_KEY:
            return []
        if not outbound_date:
            outbound_date = (date.today() + timedelta(days=7)).isoformat()

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                _SERPAPI_BASE,
                params={
                    "engine": "google_flights",
                    "departure_id": departure_id,
                    "arrival_id": arrival_id,
                    "outbound_date": outbound_date,
                    "type": "2",        # one-way
                    "currency": "USD",
                    "hl": "en",
                    "api_key": SERPAPI_KEY,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        best = data.get("best_flights", [])
        others = data.get("other_flights", [])
        candidates = sorted(best + others, key=lambda f: f.get("price") or 99999)
        return [_format_option(c, departure_id, arrival_id) for c in candidates[:max_options]]
