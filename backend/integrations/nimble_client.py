"""
Nimble SERP client — open-web data for venue discovery.

Two engines are used in parallel per query:

  google_maps  → local pack results: Place IDs, coordinates, ratings,
                 business descriptions.  Richer venue coverage than
                 Google Places alone; Place IDs can be stored.

  google_search → organic web results (Yelp, TripAdvisor, Time Out, etc.)
                  for richer review snippets that Claude uses for signal
                  extraction.  Only the title + snippet text is consumed;
                  raw URLs are never persisted.

AUTH: HTTP Basic — API key as username, empty password.

GRACEFUL DEGRADATION: When NIMBLE_API_KEY is absent all methods return
empty lists so the scraper falls back to Google Places only.
"""
from __future__ import annotations

import base64
import os
from typing import Any

import httpx

from tracing import http_span

NIMBLE_API_KEY = os.environ.get("NIMBLE_API_KEY", "")
_BASE = "https://api.webit.live"
_SERP = "/api/v1/realtime/serp"


def _auth_header() -> str:
    token = base64.b64encode(f"{NIMBLE_API_KEY}:".encode()).decode()
    return f"Basic {token}"


class NimbleClient:
    """
    Async Nimble SERP client.
    Use as an async context manager or call close() explicitly.
    """

    def __init__(self) -> None:
        if not NIMBLE_API_KEY:
            self._http: httpx.AsyncClient | None = None
            return
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            headers={
                "Authorization": _auth_header(),
                "Content-Type": "application/json",
            },
            timeout=8.0,
        )

    # ── google_maps engine ────────────────────────────────────────────────────

    async def maps_search(self, query: str, location: str = "", country: str = "") -> list[dict]:
        """
        Nimble google_maps SERP — returns local pack results.

        Each result dict has the same keys as GoogleMapsClient.search_venues:
          place_id, name, address, latitude, longitude,
          rating, price_per_head_usd, snippet, url, source

        country: ISO 3166-1 alpha-2 code (e.g. "US") — restricts results to
        that country.  Without this, Nimble may return results from abroad.
        """
        if not self._http:
            return []

        locale = f"en-{country}" if country else "en"
        payload: dict[str, Any] = {
            "parse": True,
            "search_engine_type": "google_maps",
            "query": query,
            "locale": locale,
        }
        if country:
            payload["country"] = country
        if location:
            payload["geo"] = location

        with http_span(
            "therightspot.nimble_maps",
            "nimble",
            url=f"{_BASE}{_SERP}",
            method="POST",
            **{"search.query": query[:100]},
        ) as span:
            try:
                resp = await self._http.post(_SERP, json=payload)
                resp.raise_for_status()
                span.set_tag("http.status_code", resp.status_code)
            except Exception as exc:
                span.set_tag("error", str(exc))
                return []

            data = resp.json()
            local = data.get("local_results") or data.get("places_results") or []
            span.set_tag("results.count", len(local))

            results = []
            for r in local:
                coords = r.get("gps_coordinates") or {}
                results.append({
                    "place_id": r.get("place_id", ""),
                    "name": r.get("title", r.get("name", "")),
                    "address": r.get("address", ""),
                    "latitude": coords.get("latitude") or r.get("latitude"),
                    "longitude": coords.get("longitude") or r.get("longitude"),
                    "rating": r.get("rating"),
                    "price_per_head_usd": 0,
                    "snippet": r.get("description", r.get("snippet", "")),
                    "url": r.get("link", r.get("website", "")),
                    "source": "nimble_maps",
                })
            return results

    # ── google_search engine ──────────────────────────────────────────────────

    async def serp_search(self, query: str, country: str = "") -> list[dict]:
        """
        Nimble google_search SERP — organic web results.

        Returns list of dicts with keys: name, snippet, url, source.
        The snippet (from Yelp, TripAdvisor, Time Out, etc.) is passed to
        Claude for signal extraction.  No coordinates — these supplement
        maps results rather than replacing them.

        country: ISO 3166-1 alpha-2 code (e.g. "US") — restricts organic
        results to that country's Google index.
        """
        if not self._http:
            return []

        locale = f"en-{country}" if country else "en"
        payload: dict[str, Any] = {
            "parse": True,
            "search_engine_type": "google_search",
            "query": query,
            "locale": locale,
        }
        if country:
            payload["country"] = country

        with http_span(
            "therightspot.nimble_serp",
            "nimble",
            url=f"{_BASE}{_SERP}",
            method="POST",
            **{"search.query": query[:100]},
        ) as span:
            try:
                resp = await self._http.post(_SERP, json=payload)
                resp.raise_for_status()
                span.set_tag("http.status_code", resp.status_code)
            except Exception as exc:
                span.set_tag("error", str(exc))
                return []

            data = resp.json()
            organic = data.get("organic_results", [])
            span.set_tag("results.count", len(organic))

            results = []
            for r in organic:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                if not title or not snippet:
                    continue
                results.append({
                    "name": title,
                    "snippet": snippet,
                    "url": r.get("link", ""),
                    "source": "nimble_serp",
                })
            return results

    async def __aenter__(self) -> "NimbleClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
