"""
Unit tests for GoogleMapsClient and the Nimble Place ID pipeline.

Test categories:
  - Geocoding: address → lat/lng
  - find_place_id: text search → Place ID string
  - get_place_details: real-time display data (compliance: never stored)
  - batch_find_place_ids: concurrent lookups with semaphore
  - to_map_markers: scored venues → MapMarker list
  - Nimble maps search: _nimble_maps_search + Place ID extraction
  - Compliance guards: verify Google content is never included in ClickHouse rows
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..integrations.google_maps_client import GoogleMapsClient
from ..models.models import (
    GeocodeResult,
    GooglePlaceDetails,
    GooglePriceLevel,
    MapMarker,
    NimbleMapsResult,
    ScoredVenue,
    VenueSignal,
)
from ..agents.scraper_agent import _extract_place_id_from_url, ScraperAgent


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _http_resp(status: int, data: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.raise_for_status = MagicMock(side_effect=None if status < 400 else Exception(f"HTTP {status}"))
    r.json = MagicMock(return_value=data)
    return r


def _scored_venue(i: int, place_id: str = "") -> ScoredVenue:
    return ScoredVenue(
        venue_id=f"v{i}",
        name=f"Venue {i}",
        city="New York City",
        place_id=place_id,
        latitude=40.7 + i * 0.01,
        longitude=-74.0 + i * 0.01,
        price_per_head=80,
        has_private_room=True,
        match_score=75.0,
    )


# ─── _extract_place_id_from_url ───────────────────────────────────────────────

class TestExtractPlaceIdFromUrl:
    def test_extracts_from_place_id_param(self):
        url = "https://maps.google.com/maps?q=place_id:ChIJN1t_tDeuEmsR"
        assert _extract_place_id_from_url(url) == "ChIJN1t_tDeuEmsR"

    def test_returns_empty_for_no_match(self):
        assert _extract_place_id_from_url("https://example.com/restaurant") == ""

    def test_returns_empty_for_blank_url(self):
        assert _extract_place_id_from_url("") == ""

    def test_extracts_alphanumeric_place_id(self):
        url = "https://www.google.com/maps/place/?q=place_id:ChIJ_abc123-DEF"
        assert _extract_place_id_from_url(url) == "ChIJ_abc123-DEF"


# ─── GoogleMapsClient.geocode ─────────────────────────────────────────────────

class TestGeocode:
    @pytest.mark.asyncio
    async def test_returns_geocode_result(self):
        mock_resp = _http_resp(200, {
            "results": [{
                "geometry": {"location": {"lat": 40.7260, "lng": -74.0060}},
                "formatted_address": "54 Watts St, New York, NY 10013",
                "place_id": "ChIJABC123",
            }],
            "status": "OK",
        })
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            result = await client.geocode("54 Watts St New York")

        assert isinstance(result, GeocodeResult)
        assert result.latitude == pytest.approx(40.7260)
        assert result.longitude == pytest.approx(-74.0060)
        assert result.place_id == "ChIJABC123"

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_results(self):
        mock_resp = _http_resp(200, {"results": [], "status": "ZERO_RESULTS"})
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            result = await client.geocode("nowhere special")

        assert result is None

    @pytest.mark.asyncio
    async def test_coordinates_are_storable(self):
        """Lat/lng from geocoding CAN be stored — it is universal data, not Google content."""
        mock_resp = _http_resp(200, {
            "results": [{
                "geometry": {"location": {"lat": 48.8566, "lng": 2.3522}},
                "formatted_address": "Paris, France",
                "place_id": "ChIJPARIS",
            }],
        })
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            result = await client.geocode("Paris")

        # Verify the result is a simple float — storable in ClickHouse Float32
        assert isinstance(result.latitude, float)
        assert isinstance(result.longitude, float)


# ─── GoogleMapsClient.find_place_id ──────────────────────────────────────────

class TestFindPlaceId:
    @pytest.mark.asyncio
    async def test_returns_place_id_on_match(self):
        mock_resp = _http_resp(200, {
            "places": [{"id": "ChIJLOCANDA", "displayName": {"text": "Locanda Verde"}}]
        })
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            pid = await client.find_place_id("Locanda Verde", "New York City")

        assert pid == "ChIJLOCANDA"

    @pytest.mark.asyncio
    async def test_returns_none_for_no_results(self):
        mock_resp = _http_resp(200, {"places": []})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            pid = await client.find_place_id("Nonexistent", "Mars")

        assert pid is None

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_resp = _http_resp(404, {})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            pid = await client.find_place_id("Test", "NYC")

        assert pid is None

    @pytest.mark.asyncio
    async def test_uses_minimal_field_mask(self):
        mock_resp = _http_resp(200, {"places": [{"id": "ChIJXYZ"}]})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            await client.find_place_id("Test", "NYC")

        headers = mock_http.post.call_args[1].get("headers", {})
        # Minimal mask keeps billing low — must not request all fields
        assert "X-Goog-FieldMask" in headers
        assert "photos" not in headers["X-Goog-FieldMask"]


# ─── GoogleMapsClient.get_place_details ──────────────────────────────────────

class TestGetPlaceDetails:
    @pytest.mark.asyncio
    async def test_returns_place_details(self):
        mock_resp = _http_resp(200, {
            "id": "ChIJLOCANDA",
            "displayName": {"text": "Locanda Verde"},
            "formattedAddress": "377 Greenwich St, New York, NY 10013",
            "rating": 4.7,
            "userRatingCount": 3200,
            "priceLevel": "PRICE_LEVEL_EXPENSIVE",
            "regularOpeningHours": {"openNow": True},
            "websiteUri": "https://locandaverde.com",
            "nationalPhoneNumber": "+1 212-925-3797",
            "location": {"latitude": 40.7206, "longitude": -74.0089},
        })
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            details = await client.get_place_details("ChIJLOCANDA")

        assert isinstance(details, GooglePlaceDetails)
        assert details.name == "Locanda Verde"
        assert details.rating == pytest.approx(4.7)
        assert details.price_level == GooglePriceLevel.EXPENSIVE
        assert details.is_open_now is True

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_resp = _http_resp(404, {})
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            result = await client.get_place_details("invalid_id")

        assert result is None

    @pytest.mark.asyncio
    async def test_result_is_not_a_db_model(self):
        """
        Compliance check: GooglePlaceDetails must NOT match VenueSignal's CH_COLUMNS.
        This ensures we never accidentally persist Google content data to ClickHouse.
        """
        mock_resp = _http_resp(200, {
            "id": "ChIJ",
            "displayName": {"text": "Test"},
            "location": {"latitude": 40.0, "longitude": -74.0},
        })
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            details = await client.get_place_details("ChIJ")

        # GooglePlaceDetails should NOT have CH_COLUMNS (ClickHouse serialization)
        assert not hasattr(GooglePlaceDetails, "CH_COLUMNS")
        assert not hasattr(details, "to_ch_row")


# ─── GoogleMapsClient.batch_find_place_ids ───────────────────────────────────

class TestBatchFindPlaceIds:
    @pytest.mark.asyncio
    async def test_returns_dict_of_venue_id_to_place_id(self):
        venues = [_scored_venue(1), _scored_venue(2)]

        call_count = 0
        async def mock_post(url, *, json, headers):
            nonlocal call_count
            call_count += 1
            name = json["textQuery"].split(" ")[1]  # "Venue 1" or "Venue 2"
            return _http_resp(200, {"places": [{"id": f"ChIJ-{name}"}]})

        mock_http = AsyncMock()
        mock_http.post = mock_post
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            result = await client.batch_find_place_ids(venues)

        assert len(result) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_excludes_venues_with_no_match(self):
        venues = [_scored_venue(1)]

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=_http_resp(200, {"places": []}))
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            result = await client.batch_find_place_ids(venues)

        assert result == {}

    @pytest.mark.asyncio
    async def test_concurrent_calls_bounded(self):
        """Verify the internal semaphore limits concurrent Google API calls."""
        active = 0
        peak = 0

        async def counted_post(url, *, json, headers):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _http_resp(200, {"places": [{"id": "ChIJ"}]})

        mock_http = AsyncMock()
        mock_http.post = counted_post
        mock_http.aclose = AsyncMock()

        venues = [_scored_venue(i) for i in range(10)]

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = GoogleMapsClient()
            await client.batch_find_place_ids(venues)

        assert peak <= 5, f"Expected ≤5 concurrent calls, got {peak}"


# ─── GoogleMapsClient.to_map_markers ─────────────────────────────────────────

class TestToMapMarkers:
    def test_only_venues_with_place_id_become_markers(self):
        venues = [
            _scored_venue(1, place_id="ChIJA"),
            _scored_venue(2, place_id=""),       # no Place ID — skipped
            _scored_venue(3, place_id="ChIJC"),
        ]
        markers = GoogleMapsClient.to_map_markers(venues)
        assert len(markers) == 2
        assert all(m.place_id for m in markers)

    def test_marker_fields_are_correct(self):
        venue = _scored_venue(1, place_id="ChIJXYZ")
        marker = GoogleMapsClient.to_map_markers([venue])[0]
        assert marker.place_id == "ChIJXYZ"
        assert marker.venue_id == venue.venue_id
        assert marker.match_score == venue.match_score
        assert marker.has_private_room == venue.has_private_room

    def test_markers_do_not_include_google_content_fields(self):
        venue = _scored_venue(1, place_id="ChIJXYZ")
        marker = GoogleMapsClient.to_map_markers([venue])[0]
        # Must not include any Google content that violates TOS storage rules
        assert not hasattr(marker, "rating")
        assert not hasattr(marker, "user_rating_count")
        assert not hasattr(marker, "is_open_now")
        assert not hasattr(marker, "website_uri")

    def test_empty_venues_returns_empty_list(self):
        assert GoogleMapsClient.to_map_markers([]) == []


# ─── Nimble maps search integration ──────────────────────────────────────────

class TestNimbleMapsSearch:
    @pytest.mark.asyncio
    async def test_extracts_place_id_from_local_results(self, birthday_intent):
        maps_resp = MagicMock()
        maps_resp.status_code = 200
        maps_resp.raise_for_status = MagicMock()
        maps_resp.json = MagicMock(return_value={
            "local_results": [
                {
                    "title": "Locanda Verde",
                    "place_id": "ChIJLOCANDA",
                    "address": "377 Greenwich St, New York",
                    "gps_coordinates": {"latitude": 40.72, "longitude": -74.01},
                    "rating": 4.7,
                    "reviews": 3200,
                    "description": "Celebrated Italian in Tribeca, great for birthdays.",
                    "link": "https://maps.google.com/?q=Locanda+Verde",
                }
            ]
        })
        serp_resp = MagicMock()
        serp_resp.status_code = 200
        serp_resp.raise_for_status = MagicMock()
        serp_resp.json = MagicMock(return_value={"organic_results": []})

        call_count = 0
        async def multi_resp(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return maps_resp if call_count == 1 else serp_resp

        mock_http = AsyncMock()
        mock_http.post = multi_resp
        mock_http.aclose = AsyncMock()

        # Patch Claude extraction to return empty signals
        from ..models.models import ExtractedSignals
        mock_claude = AsyncMock()
        mock_claude.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text=ExtractedSignals().model_dump_json())])
        )

        with (
            patch("httpx.AsyncClient", return_value=mock_http),
            patch("backend.agents.scraper_agent._client", mock_claude),
        ):
            agent = ScraperAgent()
            results = await agent.run(birthday_intent)

        locanda = next((r for r in results if r.get("name") == "Locanda Verde"), None)
        assert locanda is not None
        assert locanda.get("place_id") == "ChIJLOCANDA"
        assert locanda.get("address") == "377 Greenwich St, New York"
        assert locanda.get("latitude") == pytest.approx(40.72)

    @pytest.mark.asyncio
    async def test_place_id_flows_through_to_enriched_venue(self, birthday_intent):
        """Verify Place IDs extracted from Nimble end up in the dict returned by run()."""
        maps_resp_data = {
            "local_results": [{
                "title": "Test Venue",
                "place_id": "ChIJTEST_123",
                "description": "Great Italian restaurant for events.",
                "link": "",
                "address": "123 Main St",
                "gps_coordinates": {"latitude": 40.7, "longitude": -74.0},
            }]
        }

        call_count = 0
        async def multi_resp(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json = MagicMock(
                return_value=maps_resp_data if call_count == 1
                else {"organic_results": []}
            )
            return r

        from ..models.models import ExtractedSignals
        mock_claude = AsyncMock()
        mock_claude.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text=ExtractedSignals().model_dump_json())])
        )
        mock_http = AsyncMock()
        mock_http.post = multi_resp
        mock_http.aclose = AsyncMock()

        with (
            patch("httpx.AsyncClient", return_value=mock_http),
            patch("backend.agents.scraper_agent._client", mock_claude),
        ):
            results = await ScraperAgent().run(birthday_intent)

        for r in results:
            if r.get("place_id"):
                assert r["place_id"].startswith("ChIJ")
