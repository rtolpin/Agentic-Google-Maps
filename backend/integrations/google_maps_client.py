"""
Google Maps Platform client — real-time display and geocoding only.

COMPLIANCE ARCHITECTURE (Google TOS):
  - This client is used for TWO purposes only:
      1. Geocoding: convert addresses from Nimble into lat/lng for ClickHouse storage
      2. Real-time place details: fetch fresh data when the user opens a venue card
         These results are displayed on a Google Map and are NEVER cached.
  - Do NOT store GooglePlaceDetails objects in ClickHouse or Redis.
  - Do NOT display non-Google location data on a Google Map.
  - Store only the place_id string (a neutral identifier, not Google data).

DATA FLOW:
  Nimble SERP (google_maps engine)
      → extracts Place IDs + addresses at scale (no per-request Google billing)
      → stored in ClickHouse: place_id, address, latitude, longitude

  Google Maps Platform (this client)
      → geocoding: address → lat/lng (stored, not displayed)
      → place details: place_id → real-time display data (NOT stored)
      → JS API: frontend renders interactive map using stored place_ids

PRICING NOTE:
  Use field masks on every Places API call to request only the fields needed.
  Unmasked calls bill for all field categories ($5-$7 per 1,000 requests).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

from tracing import http_span
from models.models import GeocodeResult, GooglePlaceDetails, GooglePriceLevel, MapMarker, ScoredVenue

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
_PLACES_BASE = "https://places.googleapis.com/v1"
_GEOCODING_BASE = "https://maps.googleapis.com/maps/api/geocode/json"

# Field masks keep billing minimal — only request what the UI actually renders
_DISPLAY_FIELD_MASK = (
    "id,displayName,formattedAddress,rating,userRatingCount,"
    "priceLevel,regularOpeningHours,websiteUri,nationalPhoneNumber,"
    "location"
)
_FIND_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"
_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.priceLevel,places.editorialSummary,"
    "places.types,places.websiteUri"
)

# The API key is browser-restricted (HTTP referrer allowlist).
# Server-side calls must supply this header to pass the referrer check.
_SERVER_REFERER = os.environ.get("GOOGLE_MAPS_REFERER", "http://localhost:3000")

_PRICE_LEVEL_TO_USD = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 20,
    "PRICE_LEVEL_MODERATE": 50,
    "PRICE_LEVEL_EXPENSIVE": 100,
    "PRICE_LEVEL_VERY_EXPENSIVE": 175,
}


class GoogleMapsClient:
    """
    Thin async wrapper around the Google Maps Platform REST APIs.
    All methods are real-time — responses must never be stored long-term.
    """

    def __init__(self) -> None:
        self._places = httpx.AsyncClient(
            base_url=_PLACES_BASE,
            headers={
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "Content-Type": "application/json",
                "Referer": _SERVER_REFERER,
            },
            timeout=15.0,
        )
        self._geocoding = httpx.AsyncClient(timeout=10.0)

    # ─── Venue search (Place IDs + coordinates + editorial text) ─────────────

    async def search_venues(
        self,
        query: str,
        max_results: int = 15,
        location_bias: dict | None = None,
        open_now: bool = False,
    ) -> list[dict]:
        """
        Text-search for venues using the Google Places API (New).

        Returns a list of dicts with keys:
          place_id, name, address, latitude, longitude,
          rating, price_per_head_usd, snippet, url, source

        Coordinates and place_id ARE storable (neutral identifiers / universal data).
        editorial_summary is used for Claude signal extraction then discarded.

        location_bias: optional {"lat": float, "lng": float, "radius_m": float} —
        when provided, adds a locationRestriction.circle so results are strictly
        within the given radius of the coordinates. Prevents cross-country or
        cross-continent results when a city is known.
        """
        body: dict = {"textQuery": query, "maxResultCount": min(max_results, 20)}
        if open_now:
            body["openNow"] = True
        if location_bias:
            body["locationRestriction"] = {
                "circle": {
                    "center": {"latitude": location_bias["lat"], "longitude": location_bias["lng"]},
                    "radius": float(location_bias.get("radius_m", 5000.0)),
                }
            }

        with http_span(
            "therightspot.google_maps.search",
            "google_maps",
            url=f"{_PLACES_BASE}/places:searchText",
            method="POST",
            **{"search.query": query[:100]},
        ) as span:
            try:
                resp = await self._places.post(
                    "/places:searchText",
                    json=body,
                    headers={"X-Goog-FieldMask": _SEARCH_FIELD_MASK},
                )
                resp.raise_for_status()
                span.set_tag("http.status_code", resp.status_code)
            except Exception as exc:
                span.set_tag("error", str(exc))
                return []

            places = resp.json().get("places", [])
            span.set_tag("results.count", len(places))

            results = []
            for p in places:
                loc = p.get("location", {})
                price_label = p.get("priceLevel", "")
                results.append({
                    "place_id": p.get("id", ""),
                    "name": p.get("displayName", {}).get("text", ""),
                    "address": p.get("formattedAddress", ""),
                    "latitude": loc.get("latitude"),
                    "longitude": loc.get("longitude"),
                    "rating": p.get("rating"),
                    "price_per_head_usd": _PRICE_LEVEL_TO_USD.get(price_label, 0),
                    "snippet": p.get("editorialSummary", {}).get("text", ""),
                    "url": p.get("websiteUri", ""),
                    "source": "google_places",
                })
            return results

    # ─── Geocoding (results ARE stored — lat/lng is our data, not Google's) ──

    async def geocode(self, address: str) -> GeocodeResult | None:
        """
        Convert an address string into lat/lng coordinates.
        The resulting lat/lng IS stored in ClickHouse because it is a universal
        coordinate, not Google content. The place_id returned here can also be stored.
        """
        with http_span(
            "therightspot.google_maps.geocode",
            "google_maps",
            url=_GEOCODING_BASE,
            method="GET",
            address=address[:100],
        ) as span:
            resp = await self._geocoding.get(
                _GEOCODING_BASE,
                params={"address": address, "key": GOOGLE_MAPS_API_KEY},
            )
            resp.raise_for_status()
            span.set_tag("http.status_code", resp.status_code)
            data = resp.json()
            results = data.get("results", [])
            if not results:
                span.set_tag("geocode.found", False)
                return None
            r = results[0]
            loc = r.get("geometry", {}).get("location", {})
            span.set_tag("geocode.found", True)
            return GeocodeResult(
                latitude=loc.get("lat", 0.0),
                longitude=loc.get("lng", 0.0),
                formatted_address=r.get("formatted_address", ""),
                place_id=r.get("place_id", ""),
            )

    # ─── Place ID lookup (identifier only — not Google content) ──────────────

    async def find_place_id(self, name: str, city: str) -> str | None:
        """
        Find a Google Place ID for a venue by text search.
        Returns the ID string only. The ID can be stored — it is a neutral
        identifier, not Google's content data.
        """
        resp = await self._places.post(
            "/places:searchText",
            json={"textQuery": f"{name} {city}", "maxResultCount": 1},
            headers={"X-Goog-FieldMask": _FIND_FIELD_MASK},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        places = resp.json().get("places", [])
        return places[0]["id"] if places else None

    async def batch_find_place_ids(
        self, venues: list[ScoredVenue]
    ) -> dict[str, str]:
        """
        Find Place IDs for a batch of venues that don't have one yet.
        Uses a semaphore to stay within Google's QPS limits.
        """
        sem = asyncio.Semaphore(5)

        async def _find(venue: ScoredVenue) -> tuple[str, str | None]:
            async with sem:
                pid = await self.find_place_id(venue.name, venue.city)
                return venue.venue_id, pid

        pairs = await asyncio.gather(*[_find(v) for v in venues], return_exceptions=True)
        return {
            vid: pid
            for vid, pid in pairs
            if not isinstance(pid, Exception) and pid
        }

    # ─── Real-time place details (NEVER stored) ───────────────────────────────

    async def get_place_details(self, place_id: str) -> GooglePlaceDetails | None:
        """
        Fetch live place details for a venue card.

        IMPORTANT: Do NOT persist this response. Google TOS requires:
          - Results must be displayed on a Google Map
          - Data may not be cached beyond the user's session
        This endpoint is called only when the user opens a venue detail card.
        """
        with http_span(
            "therightspot.google_maps.details",
            "google_maps",
            url=f"{_PLACES_BASE}/places/{place_id}",
            method="GET",
            **{"place_id": place_id},
        ) as span:
            resp = await self._places.get(
                f"/places/{place_id}",
                headers={"X-Goog-FieldMask": _DISPLAY_FIELD_MASK},
            )
            span.set_tag("http.status_code", resp.status_code)
            if resp.status_code == 404:
                span.set_tag("place.found", False)
                return None
            resp.raise_for_status()
            span.set_tag("place.found", True)
            d = resp.json()
            loc = d.get("location", {})
            return GooglePlaceDetails(
                place_id=d.get("id", place_id),
                name=d.get("displayName", {}).get("text", ""),
                formatted_address=d.get("formattedAddress", ""),
                rating=d.get("rating"),
                user_rating_count=d.get("userRatingCount"),
                price_level=GooglePriceLevel(d["priceLevel"]) if d.get("priceLevel") else None,
                is_open_now=d.get("regularOpeningHours", {}).get("openNow"),
                website_uri=d.get("websiteUri"),
                phone_number=d.get("nationalPhoneNumber"),
                latitude=loc.get("latitude"),
                longitude=loc.get("longitude"),
            )

    # ─── Map marker assembly ──────────────────────────────────────────────────

    @staticmethod
    def to_map_markers(venues: list[ScoredVenue]) -> list[MapMarker]:
        """
        Convert scored venues into map markers for the frontend.
        Only includes fields the Google Maps JS API needs to render pins.
        No Google content is included — the frontend calls Google directly via JS.
        """
        return [
            MapMarker(
                venue_id=v.venue_id,
                place_id=v.place_id,
                name=v.name,
                latitude=v.latitude,
                longitude=v.longitude,
                match_score=v.match_score,
                has_private_room=v.has_private_room,
                price_per_head=v.price_per_head,
            )
            for v in venues
            if v.place_id  # only venues with a verified Place ID get a marker
        ]

    async def __aenter__(self) -> "GoogleMapsClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._places.aclose()
        await self._geocoding.aclose()
