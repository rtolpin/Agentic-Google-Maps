"""
Tests for the three-layer location restriction system.

Layer 1 — _CITY_COORDS: hardcoded lat/lng for major cities so a geocoding
  failure can never silently disable locationRestriction.

Layer 2 — Nimble country + locale params: geo-targets Nimble requests to
  the correct country, preventing foreign organic results.

Layer 3 — _filter_by_location (orchestrator): removes any stale ClickHouse
  entries whose coordinates fall outside the expected city area.

Also covers the 0-venues regression: the filter must run BEFORE the
in-memory fallback check, so "all ClickHouse results were stale and removed"
correctly triggers the fallback against fresh scraper data.

And the openNow semantics fix: "open today" means operating today (not open
at this exact moment), so it must NOT trigger openNow=true in the Google
Places API — which returned 0 results after business hours.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..agents.scraper_agent import (
    _CITY_COORDS,
    _OPEN_NOW_KEYWORDS,
    _OPEN_TODAY_KEYWORDS,
)
from ..agents.orchestrator import (
    _filter_by_location,
    _haversine_m,
    _score_enriched_fallback,
)
from ..models.models import ScoredVenue, VenueIntent

# ── reference coordinates ─────────────────────────────────────────────────────
NYC_LAT, NYC_LNG = 40.7128, -74.0060        # Manhattan center
MOMA_LAT, MOMA_LNG = 40.7614, -73.9776      # ~8 km from center
MET_LAT, MET_LNG = 40.7794, -73.9632        # ~9 km from center
LONDON_LAT, LONDON_LNG = 51.5074, -0.1278   # >5,000 km away
PHOENIX_LAT, PHOENIX_LNG = 33.4484, -112.074  # wrong US city


def _venue(name: str, lat=None, lng=None, city="New York City") -> ScoredVenue:
    return ScoredVenue(
        venue_id=name.lower().replace(" ", "_"),
        name=name,
        city=city,
        latitude=lat,
        longitude=lng,
        match_score=70.0,
    )


# ─── Layer 1: _CITY_COORDS ────────────────────────────────────────────────────

class TestCityCoords:
    def test_nyc_variants_all_present(self):
        for key in ("New York City", "New York", "NYC", "Manhattan", "Brooklyn"):
            assert key in _CITY_COORDS, f"{key!r} missing from _CITY_COORDS"

    def test_major_us_cities_present(self):
        required = [
            "Los Angeles", "San Francisco", "Chicago", "Seattle",
            "Boston", "Austin", "Denver", "Miami", "Atlanta",
            "Dallas", "Houston", "Washington DC", "Philadelphia",
            "Las Vegas", "Nashville", "New Orleans",
        ]
        for city in required:
            assert city in _CITY_COORDS, f"{city!r} missing from _CITY_COORDS"

    def test_all_entries_have_us_country_code(self):
        for city, (lat, lng, country) in _CITY_COORDS.items():
            assert country == "US", (
                f"{city!r} has unexpected country code {country!r}"
            )

    def test_nyc_coords_are_plausible(self):
        lat, lng, country = _CITY_COORDS["New York City"]
        assert 40.0 < lat < 41.5, f"NYC lat {lat} looks wrong"
        assert -75.0 < lng < -73.0, f"NYC lng {lng} looks wrong"
        assert country == "US"

    def test_la_coords_are_plausible(self):
        lat, lng, _ = _CITY_COORDS["Los Angeles"]
        assert 33.0 < lat < 35.0
        assert -119.0 < lng < -117.0

    def test_sf_coords_are_plausible(self):
        lat, lng, _ = _CITY_COORDS["San Francisco"]
        assert 37.0 < lat < 38.5
        assert -123.0 < lng < -121.5

    def test_chicago_coords_are_plausible(self):
        lat, lng, _ = _CITY_COORDS["Chicago"]
        assert 41.0 < lat < 42.5
        assert -88.5 < lng < -87.0

    def test_nyc_and_manhattan_coords_are_close(self):
        nyc_lat, nyc_lng, _ = _CITY_COORDS["New York City"]
        man_lat, man_lng, _ = _CITY_COORDS["Manhattan"]
        # Should be within ~10 km of each other
        assert abs(nyc_lat - man_lat) < 0.15
        assert abs(nyc_lng - man_lng) < 0.15

    def test_unknown_city_not_in_dict(self):
        assert _CITY_COORDS.get("Atlantis") is None
        assert _CITY_COORDS.get("London") is None
        assert _CITY_COORDS.get("Tokyo") is None

    def test_each_entry_is_three_tuple(self):
        for city, entry in _CITY_COORDS.items():
            assert len(entry) == 3, f"{city!r} entry should be (lat, lng, country)"
            lat, lng, country = entry
            assert isinstance(lat, float), f"{city!r} lat must be float"
            assert isinstance(lng, float), f"{city!r} lng must be float"
            assert isinstance(country, str) and len(country) == 2

    def test_no_city_has_zero_coords(self):
        for city, (lat, lng, _) in _CITY_COORDS.items():
            assert lat != 0.0 and lng != 0.0, f"{city!r} has zero coordinates"


# ─── Layer 1b: openNow semantics ──────────────────────────────────────────────

class TestOpenNowKeywords:
    """
    'open today' = operating today (shows hours today, not necessarily open NOW).
    'open now'   = literally open at this exact moment.
    Only the latter should set openNow=true in the Places API.
    """

    def test_open_today_not_in_open_now_keywords(self):
        assert "open today" not in _OPEN_NOW_KEYWORDS

    def test_open_this_weekend_not_in_open_now_keywords(self):
        assert "open this weekend" not in _OPEN_NOW_KEYWORDS

    def test_open_this_week_not_in_open_now_keywords(self):
        assert "open this week" not in _OPEN_NOW_KEYWORDS

    def test_open_now_in_open_now_keywords(self):
        assert "open now" in _OPEN_NOW_KEYWORDS

    def test_open_right_now_in_open_now_keywords(self):
        assert "open right now" in _OPEN_NOW_KEYWORDS

    def test_currently_open_in_open_now_keywords(self):
        assert "currently open" in _OPEN_NOW_KEYWORDS

    def test_open_today_in_open_today_keywords(self):
        assert "open today" in _OPEN_TODAY_KEYWORDS

    def test_open_this_weekend_in_open_today_keywords(self):
        assert "open this weekend" in _OPEN_TODAY_KEYWORDS

    def test_keyword_sets_are_disjoint(self):
        overlap = _OPEN_NOW_KEYWORDS & _OPEN_TODAY_KEYWORDS
        assert not overlap, f"Keywords appear in both sets: {overlap}"

    def test_museums_open_today_query_does_not_trigger_open_now(self):
        """Regression: 'museums and galleries open today' returned 0 results
        because openNow=true caused the Places API to return nothing after
        museum hours."""
        intent = VenueIntent(
            occasion="sightseeing",
            city="New York City",
            other_signals=["museums", "galleries", "open today"],
        )
        all_signals_lower = " ".join(
            [intent.occasion] + (intent.other_signals or [])
        ).lower()
        open_now = any(kw in all_signals_lower for kw in _OPEN_NOW_KEYWORDS)
        assert not open_now, (
            "'open today' must not set openNow=true — Museums close at night, "
            "which returns 0 venues during off-hours"
        )

    def test_open_now_query_triggers_open_now(self):
        intent = VenueIntent(
            occasion="dining",
            city="New York City",
            other_signals=["open now"],
        )
        all_signals_lower = " ".join(
            [intent.occasion] + (intent.other_signals or [])
        ).lower()
        open_now = any(kw in all_signals_lower for kw in _OPEN_NOW_KEYWORDS)
        assert open_now

    def test_currently_open_query_triggers_open_now(self):
        intent = VenueIntent(
            occasion="dining",
            city="New York City",
            other_signals=["currently open"],
        )
        all_signals_lower = " ".join(
            [intent.occasion] + (intent.other_signals or [])
        ).lower()
        open_now = any(kw in all_signals_lower for kw in _OPEN_NOW_KEYWORDS)
        assert open_now


# ─── _haversine_m ─────────────────────────────────────────────────────────────

class TestHaversineDistance:
    def test_same_point_returns_zero(self):
        assert _haversine_m(NYC_LAT, NYC_LNG, NYC_LAT, NYC_LNG) == pytest.approx(0.0, abs=1.0)

    def test_nyc_to_london_over_5000km(self):
        dist = _haversine_m(NYC_LAT, NYC_LNG, LONDON_LAT, LONDON_LNG)
        assert dist > 5_000_000

    def test_nyc_center_to_moma_under_10km(self):
        dist = _haversine_m(NYC_LAT, NYC_LNG, MOMA_LAT, MOMA_LNG)
        assert dist < 10_000

    def test_nyc_center_to_met_under_12km(self):
        dist = _haversine_m(NYC_LAT, NYC_LNG, MET_LAT, MET_LNG)
        assert dist < 12_000

    def test_nyc_to_london_exceeds_35km_filter(self):
        dist = _haversine_m(NYC_LAT, NYC_LNG, LONDON_LAT, LONDON_LNG)
        assert dist > 35_000

    def test_nyc_to_phoenix_exceeds_35km_filter(self):
        dist = _haversine_m(NYC_LAT, NYC_LNG, PHOENIX_LAT, PHOENIX_LNG)
        assert dist > 35_000

    def test_nyc_center_to_brooklyn_within_35km(self):
        brooklyn_lat, brooklyn_lng, _ = _CITY_COORDS["Brooklyn"]
        dist = _haversine_m(NYC_LAT, NYC_LNG, brooklyn_lat, brooklyn_lng)
        assert dist < 35_000

    def test_symmetric(self):
        d1 = _haversine_m(NYC_LAT, NYC_LNG, LONDON_LAT, LONDON_LNG)
        d2 = _haversine_m(LONDON_LAT, LONDON_LNG, NYC_LAT, NYC_LNG)
        assert d1 == pytest.approx(d2, rel=1e-6)


# ─── Layer 3: _filter_by_location ─────────────────────────────────────────────

class TestFilterByLocation:
    """_filter_by_location is the orchestrator-level backstop that removes stale
    ClickHouse entries with wrong coordinates."""

    @pytest.mark.asyncio
    async def test_removes_london_venue_for_nyc_gps_search(self):
        venues = [_venue("British Museum", LONDON_LAT, LONDON_LNG)]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=NYC_LAT, user_lng=NYC_LNG, user_radius_m=5000.0,
        )
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_keeps_moma_for_nyc_gps_search(self):
        venues = [_venue("MoMA", MOMA_LAT, MOMA_LNG)]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=NYC_LAT, user_lng=NYC_LNG, user_radius_m=5000.0,
        )
        assert len(result) == 1
        assert result[0].name == "MoMA"

    @pytest.mark.asyncio
    async def test_keeps_venue_without_coordinates(self):
        """Venues with no lat/lng are kept — they show in list but not on map."""
        venues = [_venue("No Coords Museum", lat=None, lng=None)]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=NYC_LAT, user_lng=NYC_LNG, user_radius_m=5000.0,
        )
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_removes_london_for_nyc_city_search_no_geocoding(self):
        """NYC is in _CITY_COORDS so GoogleMapsClient.geocode must NOT be called."""
        venues = [_venue("British Museum", LONDON_LAT, LONDON_LNG)]
        intent = VenueIntent(city="New York City")

        with patch("backend.agents.orchestrator.GoogleMapsClient") as mock_gc:
            result = await _filter_by_location(
                venues, intent,
                user_lat=None, user_lng=None, user_radius_m=None,
            )

        mock_gc.assert_not_called()
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_keeps_moma_for_nyc_city_search(self):
        venues = [_venue("MoMA", MOMA_LAT, MOMA_LNG)]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_keeps_met_for_nyc_city_search(self):
        venues = [_venue("The Metropolitan Museum of Art", MET_LAT, MET_LNG)]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_removes_phoenix_venue_for_nyc_search(self):
        venues = [_venue("Phoenix Art Museum", PHOENIX_LAT, PHOENIX_LNG)]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_mixed_local_and_foreign_venues(self):
        venues = [
            _venue("MoMA", MOMA_LAT, MOMA_LNG),
            _venue("British Museum", LONDON_LAT, LONDON_LNG),
            _venue("Phoenix Art Museum", PHOENIX_LAT, PHOENIX_LNG),
            _venue("The Met", MET_LAT, MET_LNG),
            _venue("No Coords Gallery"),
        ]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        names = {v.name for v in result}
        assert "MoMA" in names
        assert "The Met" in names
        assert "No Coords Gallery" in names  # kept — no coords
        assert "British Museum" not in names
        assert "Phoenix Art Museum" not in names

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            [], intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_unknown_city_no_gps_returns_all_unfiltered(self):
        """No city, no GPS → cannot filter, return everything."""
        venues = [
            _venue("British Museum", LONDON_LAT, LONDON_LNG),
            _venue("MoMA", MOMA_LAT, MOMA_LNG),
        ]
        intent = VenueIntent(city="Unknown")
        result = await _filter_by_location(
            venues, intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert len(result) == 2  # can't filter without a reference point

    @pytest.mark.asyncio
    async def test_gps_search_radius_respected(self):
        """A small 1 km GPS radius should exclude venues >2 km away."""
        venues = [
            _venue("Nearby", NYC_LAT + 0.005, NYC_LNG + 0.005),   # ~700 m away
            _venue("Far", NYC_LAT + 0.1, NYC_LNG + 0.1),           # ~13 km away
        ]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            venues, intent,
            user_lat=NYC_LAT, user_lng=NYC_LNG, user_radius_m=1000.0,
        )
        # filter uses 2× radius as buffer, so 1000 m → 2000 m effective
        names = {v.name for v in result}
        assert "Nearby" in names
        assert "Far" not in names


# ─── 0-venues regression (stale ClickHouse → fallback) ───────────────────────

class TestStaleCHFallback:
    """
    Regression test for the 0-venues bug.

    Sequence that caused 0 venues:
    1. ClickHouse has stale entries with UK/Arizona coords (pre-fix data).
    2. score_venues() returns those stale entries — non-empty, so fallback
       was not triggered.
    3. _filter_by_location removes all of them (correctly).
    4. Result: 0 venues, even though the scraper returned valid NYC venues.

    Fix: filter runs BEFORE the fallback check, so "all filtered = 0" correctly
    falls through to the in-memory fallback from fresh scraper data.
    """

    def _stale_ch_venues(self) -> list[ScoredVenue]:
        return [
            _venue("British Museum", LONDON_LAT, LONDON_LNG),
            _venue("National Gallery", 51.5089, -0.1283),
            _venue("Tate Modern", 51.5076, -0.0994),
        ]

    def _fresh_scraper_venues(self) -> list[dict]:
        return [
            {
                "name": "The Metropolitan Museum of Art",
                "city": "New York City",
                "latitude": MET_LAT,
                "longitude": MET_LNG,
                "place_id": "ChIJMet001",
                "snippet": "World-class art museum on the Upper East Side.",
                "address": "1000 5th Ave, New York, NY",
                "url": "https://metmuseum.org",
                "source": "google_places",
            },
            {
                "name": "Museum of Modern Art",
                "city": "New York City",
                "latitude": MOMA_LAT,
                "longitude": MOMA_LNG,
                "place_id": "ChIJMoMA001",
                "snippet": "Modern and contemporary art in Midtown Manhattan.",
                "address": "11 W 53rd St, New York, NY",
                "url": "https://moma.org",
                "source": "google_places",
            },
        ]

    @pytest.mark.asyncio
    async def test_stale_ch_all_filtered_then_fallback_produces_nyc_results(self):
        """Core regression test: stale UK data → filtered → fallback wins."""
        intent = VenueIntent(city="New York City", occasion="sightseeing")

        # Step A: filter stale ClickHouse data — expect 0 survivors
        filtered_ch = await _filter_by_location(
            self._stale_ch_venues(), intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert len(filtered_ch) == 0, "All UK venues must be filtered out"

        # Step B: fallback from fresh scraper data
        fallback = _score_enriched_fallback(self._fresh_scraper_venues(), intent)
        assert len(fallback) > 0, "In-memory fallback must produce results"

        # Step C: filter fallback results (NYC venues should survive)
        final = await _filter_by_location(
            fallback, intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert len(final) > 0, "NYC venues must survive the final filter"
        names = {v.name for v in final}
        assert "The Metropolitan Museum of Art" in names or "Museum of Modern Art" in names

    @pytest.mark.asyncio
    async def test_valid_ch_venues_are_not_filtered(self):
        """When ClickHouse already has correct NYC venues, they pass through."""
        good_ch = [
            _venue("MoMA", MOMA_LAT, MOMA_LNG),
            _venue("The Met", MET_LAT, MET_LNG),
        ]
        intent = VenueIntent(city="New York City")
        filtered = await _filter_by_location(
            good_ch, intent,
            user_lat=None, user_lng=None, user_radius_m=None,
        )
        assert len(filtered) == 2

    @pytest.mark.asyncio
    async def test_empty_ch_empty_scraper_returns_zero_no_crash(self):
        intent = VenueIntent(city="New York City")
        filtered = await _filter_by_location(
            [], intent, user_lat=None, user_lng=None, user_radius_m=None,
        )
        fallback = _score_enriched_fallback([], intent)
        assert filtered == []
        assert fallback == []

    def test_score_enriched_fallback_assigns_venue_ids(self):
        """Raw scraper dicts have no venue_id — fallback must generate one."""
        intent = VenueIntent(city="New York City")
        results = _score_enriched_fallback(self._fresh_scraper_venues(), intent)
        for v in results:
            assert v.venue_id, f"venue_id must not be empty for {v.name!r}"

    def test_score_enriched_fallback_scores_are_valid(self):
        intent = VenueIntent(city="New York City", occasion="sightseeing")
        results = _score_enriched_fallback(self._fresh_scraper_venues(), intent)
        for v in results:
            assert 0 <= v.match_score <= 100, (
                f"{v.name}: match_score {v.match_score} out of range"
            )

    def test_score_enriched_fallback_sorted_descending(self):
        intent = VenueIntent(city="New York City")
        results = _score_enriched_fallback(self._fresh_scraper_venues(), intent)
        scores = [v.match_score for v in results]
        assert scores == sorted(scores, reverse=True)

    def test_score_enriched_fallback_preserves_coordinates(self):
        intent = VenueIntent(city="New York City")
        results = _score_enriched_fallback(self._fresh_scraper_venues(), intent)
        by_name = {v.name: v for v in results}
        met = by_name.get("The Metropolitan Museum of Art")
        if met:
            assert met.latitude == pytest.approx(MET_LAT, abs=0.001)
            assert met.longitude == pytest.approx(MET_LNG, abs=0.001)

    @pytest.mark.asyncio
    async def test_mixed_ch_stale_and_good_filters_correctly(self):
        """ClickHouse has both good NYC and stale UK venues — only NYC survive."""
        mixed = [
            _venue("MoMA", MOMA_LAT, MOMA_LNG),
            _venue("British Museum", LONDON_LAT, LONDON_LNG),
            _venue("The Met", MET_LAT, MET_LNG),
        ]
        intent = VenueIntent(city="New York City")
        result = await _filter_by_location(
            mixed, intent, user_lat=None, user_lng=None, user_radius_m=None,
        )
        names = {v.name for v in result}
        assert "MoMA" in names
        assert "The Met" in names
        assert "British Museum" not in names
        assert len(result) == 2


# ─── Layer 2: Nimble geo-targeting ───────────────────────────────────────────

class TestNimbleGeoTargeting:
    """Nimble must receive country + locale params to prevent foreign results."""

    def _mock_nimble_client(self, response_json: dict):
        """Build a NimbleClient with a mocked HTTP layer."""
        from ..integrations.nimble_client import NimbleClient

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = response_json

        client = NimbleClient()
        client._http = AsyncMock()
        client._http.post = AsyncMock(return_value=mock_resp)
        return client

    @pytest.mark.asyncio
    async def test_maps_search_sends_country_us(self):
        client = self._mock_nimble_client({"local_results": []})
        await client.maps_search("museums NYC", "New York City", country="US")

        body = client._http.post.call_args.kwargs["json"]
        assert body.get("country") == "US"

    @pytest.mark.asyncio
    async def test_maps_search_sends_locale_en_us(self):
        client = self._mock_nimble_client({"local_results": []})
        await client.maps_search("museums NYC", "New York City", country="US")

        body = client._http.post.call_args.kwargs["json"]
        assert body.get("locale") == "en-US"

    @pytest.mark.asyncio
    async def test_serp_search_sends_country_us(self):
        client = self._mock_nimble_client({"organic_results": []})
        await client.serp_search("museums New York City", country="US")

        body = client._http.post.call_args.kwargs["json"]
        assert body.get("country") == "US"

    @pytest.mark.asyncio
    async def test_serp_search_sends_locale_en_us(self):
        client = self._mock_nimble_client({"organic_results": []})
        await client.serp_search("museums New York City", country="US")

        body = client._http.post.call_args.kwargs["json"]
        assert body.get("locale") == "en-US"

    @pytest.mark.asyncio
    async def test_maps_search_without_country_omits_country_key(self):
        client = self._mock_nimble_client({"local_results": []})
        await client.maps_search("museums", "New York City")  # no country arg

        body = client._http.post.call_args.kwargs["json"]
        assert "country" not in body

    @pytest.mark.asyncio
    async def test_maps_search_without_country_uses_generic_locale(self):
        client = self._mock_nimble_client({"local_results": []})
        await client.maps_search("museums", "New York City")

        body = client._http.post.call_args.kwargs["json"]
        assert body.get("locale") == "en"

    @pytest.mark.asyncio
    async def test_serp_search_without_country_omits_country_key(self):
        client = self._mock_nimble_client({"organic_results": []})
        await client.serp_search("museums New York City")

        body = client._http.post.call_args.kwargs["json"]
        assert "country" not in body

    @pytest.mark.asyncio
    async def test_maps_search_no_http_client_returns_empty(self):
        """When NIMBLE_API_KEY is absent, maps_search must return [] not crash."""
        from ..integrations.nimble_client import NimbleClient

        client = NimbleClient()
        client._http = None  # simulate missing key
        result = await client.maps_search("museums", "NYC", country="US")
        assert result == []

    @pytest.mark.asyncio
    async def test_serp_search_no_http_client_returns_empty(self):
        from ..integrations.nimble_client import NimbleClient

        client = NimbleClient()
        client._http = None
        result = await client.serp_search("museums NYC", country="US")
        assert result == []

    @pytest.mark.asyncio
    async def test_maps_search_geo_param_sent_when_location_provided(self):
        client = self._mock_nimble_client({"local_results": []})
        await client.maps_search("museums", "New York City", country="US")

        body = client._http.post.call_args.kwargs["json"]
        assert body.get("geo") == "New York City"

    @pytest.mark.asyncio
    async def test_maps_search_geo_omitted_when_no_location(self):
        client = self._mock_nimble_client({"local_results": []})
        await client.maps_search("museums")  # no location

        body = client._http.post.call_args.kwargs["json"]
        assert "geo" not in body
