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


import math

# (iata, name, lat, lng) for ~130 major commercial airports worldwide
_AIRPORT_DB: list[tuple[str, str, float, float]] = [
    # North America
    ("JFK","John F. Kennedy International Airport",40.6413,-73.7781),
    ("EWR","Newark Liberty International Airport",40.6895,-74.1745),
    ("LGA","LaGuardia Airport",40.7769,-73.8740),
    ("BOS","Boston Logan International Airport",42.3656,-71.0096),
    ("PHL","Philadelphia International Airport",39.8721,-75.2411),
    ("DCA","Ronald Reagan Washington National Airport",38.8521,-77.0377),
    ("IAD","Washington Dulles International Airport",38.9531,-77.4565),
    ("BWI","Baltimore/Washington International Airport",39.1774,-76.6684),
    ("ATL","Hartsfield-Jackson Atlanta International Airport",33.6407,-84.4277),
    ("MCO","Orlando International Airport",28.4312,-81.3081),
    ("MIA","Miami International Airport",25.7959,-80.2870),
    ("FLL","Fort Lauderdale-Hollywood International Airport",26.0726,-80.1527),
    ("TPA","Tampa International Airport",27.9772,-82.5311),
    ("CLT","Charlotte Douglas International Airport",35.2140,-80.9431),
    ("ORD","O'Hare International Airport",41.9742,-87.9073),
    ("MDW","Chicago Midway International Airport",41.7868,-87.7522),
    ("DTW","Detroit Metropolitan Wayne County Airport",42.2162,-83.3554),
    ("MSP","Minneapolis-Saint Paul International Airport",44.8848,-93.2223),
    ("STL","St. Louis Lambert International Airport",38.7487,-90.3700),
    ("MCI","Kansas City International Airport",39.2976,-94.7139),
    ("MSY","Louis Armstrong New Orleans International Airport",29.9934,-90.2580),
    ("IAH","George Bush Intercontinental Airport",29.9902,-95.3368),
    ("HOU","William P. Hobby Airport",29.6454,-95.2789),
    ("DFW","Dallas/Fort Worth International Airport",32.8998,-97.0403),
    ("DEN","Denver International Airport",39.8561,-104.6737),
    ("PHX","Phoenix Sky Harbor International Airport",33.4373,-112.0078),
    ("LAS","Harry Reid International Airport",36.0840,-115.1537),
    ("SLC","Salt Lake City International Airport",40.7899,-111.9791),
    ("PDX","Portland International Airport",45.5898,-122.5951),
    ("SEA","Seattle-Tacoma International Airport",47.4502,-122.3088),
    ("SFO","San Francisco International Airport",37.6213,-122.3790),
    ("OAK","Oakland International Airport",37.7213,-122.2208),
    ("SJC","San Jose International Airport",37.3626,-121.9290),
    ("LAX","Los Angeles International Airport",33.9425,-118.4081),
    ("SAN","San Diego International Airport",32.7338,-117.1933),
    ("SMF","Sacramento International Airport",38.6954,-121.5908),
    ("HNL","Daniel K. Inouye International Airport",21.3245,-157.9251),
    ("ANC","Ted Stevens Anchorage International Airport",61.1744,-149.9962),
    ("YYZ","Toronto Pearson International Airport",43.6777,-79.6248),
    ("YVR","Vancouver International Airport",49.1967,-123.1815),
    ("YUL","Montréal-Trudeau International Airport",45.4706,-73.7408),
    ("YYC","Calgary International Airport",51.1215,-114.0076),
    ("MEX","Mexico City International Airport",19.4363,-99.0721),
    ("CUN","Cancún International Airport",21.0365,-86.8771),
    ("GRU","São Paulo-Guarulhos International Airport",-23.4356,-46.4731),
    ("GIG","Rio de Janeiro-Galeão International Airport",-22.8099,-43.2505),
    ("EZE","Buenos Aires Ezeiza International Airport",-34.8222,-58.5358),
    ("SCL","Santiago International Airport",-33.3930,-70.7858),
    ("BOG","El Dorado International Airport",4.7016,-74.1469),
    ("LIM","Jorge Chávez International Airport",-12.0219,-77.1143),
    # Europe
    ("LHR","London Heathrow Airport",51.4775,-0.4614),
    ("LGW","London Gatwick Airport",51.1537,-0.1821),
    ("STN","London Stansted Airport",51.8860,0.2389),
    ("LTN","London Luton Airport",51.8747,-0.3683),
    ("LCY","London City Airport",51.5048,0.0553),
    ("CDG","Paris Charles de Gaulle Airport",49.0097,2.5479),
    ("ORY","Paris Orly Airport",48.7233,2.3794),
    ("AMS","Amsterdam Schiphol Airport",52.3105,4.7683),
    ("FRA","Frankfurt Airport",50.0379,8.5622),
    ("MUC","Munich Airport",48.3537,11.7750),
    ("BER","Berlin Brandenburg Airport",52.3667,13.5033),
    ("HAM","Hamburg Airport",53.6304,9.9882),
    ("DUS","Düsseldorf Airport",51.2895,6.7668),
    ("MAD","Adolfo Suárez Madrid-Barajas Airport",40.4936,-3.5668),
    ("BCN","Barcelona-El Prat Airport",41.2971,2.0785),
    ("LIS","Lisbon Humberto Delgado Airport",38.7813,-9.1359),
    ("FCO","Leonardo da Vinci-Fiumicino Airport",41.8003,12.2389),
    ("MXP","Milan Malpensa Airport",45.6306,8.7231),
    ("ATH","Athens International Airport",37.9364,23.9445),
    ("ZRH","Zurich Airport",47.4582,8.5555),
    ("GVA","Geneva Airport",46.2370,6.1090),
    ("VIE","Vienna International Airport",48.1103,16.5697),
    ("PRG","Václav Havel Airport Prague",50.1008,14.2600),
    ("WAW","Warsaw Chopin Airport",52.1657,20.9671),
    ("BUD","Budapest Ferenc Liszt International Airport",47.4298,19.2611),
    ("ARN","Stockholm Arlanda Airport",59.6519,17.9186),
    ("OSL","Oslo Gardermoen Airport",60.1976,11.1004),
    ("CPH","Copenhagen Airport",55.6180,12.6508),
    ("HEL","Helsinki-Vantaa Airport",60.3172,24.9633),
    ("DUB","Dublin Airport",53.4213,-6.2701),
    ("BRU","Brussels Airport",50.9010,4.4844),
    ("IST","Istanbul Airport",41.2753,28.7519),
    # Middle East & Africa
    ("DXB","Dubai International Airport",25.2532,55.3657),
    ("AUH","Abu Dhabi International Airport",24.4330,54.6511),
    ("DOH","Hamad International Airport",25.2609,51.6138),
    ("CAI","Cairo International Airport",30.1219,31.4056),
    ("JNB","O.R. Tambo International Airport",-26.1367,28.2411),
    ("CPT","Cape Town International Airport",-33.9648,18.6017),
    ("NBO","Jomo Kenyatta International Airport",-1.3192,36.9275),
    ("LOS","Murtala Muhammed International Airport",6.5774,3.3212),
    ("ADD","Bole International Airport",8.9779,38.7993),
    ("CMN","Mohammed V International Airport",33.3675,-7.5900),
    # Asia-Pacific
    ("NRT","Tokyo Narita International Airport",35.7720,140.3929),
    ("HND","Tokyo Haneda Airport",35.5494,139.7798),
    ("KIX","Kansai International Airport",34.4347,135.2440),
    ("ICN","Incheon International Airport",37.4602,126.4407),
    ("PEK","Beijing Capital International Airport",40.0801,116.5846),
    ("PKX","Beijing Daxing International Airport",39.5097,116.4105),
    ("PVG","Shanghai Pudong International Airport",31.1443,121.8083),
    ("HKG","Hong Kong International Airport",22.3080,113.9185),
    ("TPE","Taiwan Taoyuan International Airport",25.0777,121.2328),
    ("SIN","Singapore Changi Airport",1.3644,103.9915),
    ("BKK","Suvarnabhumi Airport",13.6811,100.7472),
    ("KUL","Kuala Lumpur International Airport",2.7456,101.7099),
    ("CGK","Soekarno-Hatta International Airport",-6.1256,106.6559),
    ("SYD","Sydney Kingsford Smith Airport",-33.9399,151.1753),
    ("MEL","Melbourne Airport",-37.6690,144.8410),
    ("BNE","Brisbane Airport",-27.3842,153.1175),
    ("PER","Perth Airport",-31.9403,115.9669),
    ("AKL","Auckland Airport",-37.0082,174.7850),
    ("DEL","Indira Gandhi International Airport",28.5665,77.1031),
    ("BOM","Chhatrapati Shivaji Maharaj International Airport",19.0896,72.8656),
    ("BLR","Kempegowda International Airport",13.1979,77.7063),
    ("HYD","Rajiv Gandhi International Airport",17.2403,78.4294),
    ("MAA","Chennai International Airport",12.9900,80.1693),
    ("CCU","Netaji Subhas Chandra Bose International Airport",22.6542,88.4467),
]


# Secondary airports that lack long-haul international service → redirect to hub
_SECONDARY_TO_HUB: dict[str, str] = {
    "LGA": "JFK",  # LaGuardia has no transatlantic service
    "LCY": "LHR",  # London City is a short-haul/business airport
    "MDW": "ORD",  # Midway is mainly domestic low-cost
    "HOU": "IAH",  # Hobby is mainly domestic
    "DCA": "IAD",  # Reagan has no international long-haul
    "ORY": "CDG",  # Orly is secondary to CDG for long-haul
    "HND": "NRT",  # Haneda is growing but Narita is the primary hub
}

_AIRPORT_BY_IATA: dict[str, tuple[str, str, float, float]] = {a[0]: a for a in _AIRPORT_DB}


def _nearest_airport(lat: float, lng: float) -> tuple[str, str]:
    """Return (iata, name) of the nearest major international airport.

    Physically closest is found first, then redirected to the hub airport
    if that airport lacks long-haul international service (e.g. LGA→JFK).
    """
    best = min(
        _AIRPORT_DB,
        key=lambda a: math.asin(math.sqrt(
            math.sin(math.radians((a[2] - lat) / 2)) ** 2
            + math.cos(math.radians(lat)) * math.cos(math.radians(a[2]))
            * math.sin(math.radians((a[3] - lng) / 2)) ** 2
        )),
    )
    iata = best[0]
    if iata in _SECONDARY_TO_HUB:
        hub_iata = _SECONDARY_TO_HUB[iata]
        hub = _AIRPORT_BY_IATA.get(hub_iata)
        if hub:
            return hub[0], hub[1]
    return best[0], best[1]


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
