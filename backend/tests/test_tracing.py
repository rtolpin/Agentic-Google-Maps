"""
Datadog APM distributed tracing tests — mock tracer assertions.

SpanRecorder replaces ddtrace's global tracer so tests capture spans
in memory with no live Datadog agent required.

Test strategy:
  - Replace `tracing.tracer` with a SpanRecorder instance
  - Run the function under test
  - Assert: span was created, correct tags were set, errors are flagged
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.tracing as tracing_module
from backend.tracing import SpanRecorder, ai_span, db_span, http_span, search_span
from backend.models.models import (
    PriceBand,
    ScoredVenue,
    VenueIntelligence,
    VenueIntent,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def recorder(monkeypatch) -> SpanRecorder:
    """Replace the module-level tracer with a SpanRecorder for every test."""
    rec = SpanRecorder()
    monkeypatch.setattr(tracing_module, "tracer", rec)
    return rec


@pytest.fixture
def birthday_intent() -> VenueIntent:
    return VenueIntent(
        occasion="birthday_dinner",
        group_size=8,
        cuisine="italian",
        noise_preference="quiet",
        needs_private_room=True,
        city="New York City",
        price_band=PriceBand.UPSCALE,
    )


@pytest.fixture
def scored_venue() -> ScoredVenue:
    return ScoredVenue(
        venue_id="locanda_verde_new_york_city",
        name="Locanda Verde",
        city="New York City",
        neighborhood="Tribeca",
        cuisine="italian",
        price_per_head=95,
        has_private_room=True,
        max_group_size=20,
        noise_level="quiet",
        birthday_score=82,
        key_quotes=["Perfect for celebrations"],
        scraped_at="2026-05-23T10:00:00",
        match_score=87.5,
    )


# ─── SpanRecorder unit tests ──────────────────────────────────────────────────

class TestSpanRecorder:
    def test_captures_span_name(self, recorder: SpanRecorder) -> None:
        with recorder.trace("my.operation"):
            pass
        assert recorder.has("my.operation")

    def test_captures_tags(self, recorder: SpanRecorder) -> None:
        with recorder.trace("my.operation") as span:
            span.set_tag("key", "value")
            span.set_tag("count", 42)
        s = recorder.first("my.operation")
        assert s is not None
        assert s.tags["key"] == "value"
        assert s.tags["count"] == 42

    def test_marks_error_on_exception(self, recorder: SpanRecorder) -> None:
        with pytest.raises(ValueError):
            with recorder.trace("failing.op"):
                raise ValueError("boom")
        s = recorder.first("failing.op")
        assert s is not None
        assert s.error == 1
        assert s.tags["error.type"] == "ValueError"
        assert "boom" in s.tags["error.message"]

    def test_span_finished_after_context(self, recorder: SpanRecorder) -> None:
        with recorder.trace("short.op"):
            pass
        assert recorder.first("short.op").finished is True

    def test_multiple_spans_accumulated(self, recorder: SpanRecorder) -> None:
        for i in range(5):
            with recorder.trace("repeated.op") as span:
                span.set_tag("index", i)
        assert len(recorder.by_name("repeated.op")) == 5

    def test_clear_resets_state(self, recorder: SpanRecorder) -> None:
        with recorder.trace("temp"):
            pass
        recorder.clear()
        assert len(recorder) == 0

    def test_error_spans_query(self, recorder: SpanRecorder) -> None:
        with pytest.raises(RuntimeError):
            with recorder.trace("bad"):
                raise RuntimeError("fail")
        with recorder.trace("good"):
            pass
        assert len(recorder.error_spans()) == 1
        assert recorder.error_spans()[0].name == "bad"


# ─── ai_span helper ───────────────────────────────────────────────────────────

class TestAiSpan:
    def test_creates_llm_span(self, recorder: SpanRecorder) -> None:
        with ai_span("therightspot.test_ai_op"):
            pass
        s = recorder.first("therightspot.test_ai_op")
        assert s is not None
        assert s.span_type == "llm"

    def test_sets_model_tag(self, recorder: SpanRecorder) -> None:
        with ai_span("therightspot.test_ai_op", model="claude-opus-4-7"):
            pass
        s = recorder.first("therightspot.test_ai_op")
        assert s.tags["ai.model"] == "claude-opus-4-7"

    def test_default_model_tag(self, recorder: SpanRecorder) -> None:
        with ai_span("therightspot.test_ai_op"):
            pass
        assert recorder.first("therightspot.test_ai_op").tags["ai.model"] == "claude-sonnet-4-6"

    def test_extra_tags_propagated(self, recorder: SpanRecorder) -> None:
        with ai_span("therightspot.parse", query_length=42, city="NYC"):
            pass
        s = recorder.first("therightspot.parse")
        assert s.tags["query_length"] == 42
        assert s.tags["city"] == "NYC"

    def test_none_tags_excluded(self, recorder: SpanRecorder) -> None:
        with ai_span("therightspot.test", optional_tag=None):
            pass
        s = recorder.first("therightspot.test")
        assert "optional_tag" not in s.tags

    def test_error_propagated(self, recorder: SpanRecorder) -> None:
        with pytest.raises(RuntimeError):
            with ai_span("therightspot.failing_ai"):
                raise RuntimeError("model error")
        assert recorder.first("therightspot.failing_ai").error == 1


# ─── db_span helper ───────────────────────────────────────────────────────────

class TestDbSpan:
    def test_creates_sql_span(self, recorder: SpanRecorder) -> None:
        with db_span("therightspot.score_venues", "venue_signals"):
            pass
        s = recorder.first("therightspot.score_venues")
        assert s is not None
        assert s.span_type == "sql"

    def test_sets_db_type_tag(self, recorder: SpanRecorder) -> None:
        with db_span("therightspot.upsert", "venue_signals"):
            pass
        assert recorder.first("therightspot.upsert").tags["db.type"] == "clickhouse"

    def test_sets_table_tag(self, recorder: SpanRecorder) -> None:
        with db_span("therightspot.query", "city_benchmarks"):
            pass
        assert recorder.first("therightspot.query").tags["db.table"] == "city_benchmarks"

    def test_extra_tags_on_db_span(self, recorder: SpanRecorder) -> None:
        with db_span("therightspot.score_venues", "venue_signals", city="Tokyo", row_count=15):
            pass
        s = recorder.first("therightspot.score_venues")
        assert s.tags["city"] == "Tokyo"
        assert s.tags["row_count"] == 15


# ─── http_span helper ─────────────────────────────────────────────────────────

class TestHttpSpan:
    def test_creates_http_span(self, recorder: SpanRecorder) -> None:
        with http_span("therightspot.nimble_maps", "nimble", url="https://api.webit.live"):
            pass
        s = recorder.first("therightspot.nimble_maps")
        assert s is not None
        assert s.span_type == "http"

    def test_sets_url_tag(self, recorder: SpanRecorder) -> None:
        with http_span("therightspot.test_http", "svc", url="https://example.com/api"):
            pass
        assert recorder.first("therightspot.test_http").tags["http.url"] == "https://example.com/api"

    def test_sets_method_tag(self, recorder: SpanRecorder) -> None:
        with http_span("therightspot.test_http", "svc", method="GET"):
            pass
        assert recorder.first("therightspot.test_http").tags["http.method"] == "GET"

    def test_sets_service_tag(self, recorder: SpanRecorder) -> None:
        with http_span("therightspot.test_http", "google_maps"):
            pass
        assert recorder.first("therightspot.test_http").tags["http.service"] == "google_maps"


# ─── search_span root span ────────────────────────────────────────────────────

class TestSearchSpan:
    def test_creates_root_span(self, recorder: SpanRecorder) -> None:
        with search_span("birthday dinner for 8 in NYC", "user_abc"):
            pass
        s = recorder.first("therightspot.search")
        assert s is not None

    def test_sets_query_length_tag(self, recorder: SpanRecorder) -> None:
        query = "birthday dinner for 8"
        with search_span(query, "user_abc"):
            pass
        assert recorder.first("therightspot.search").tags["search.query_length"] == len(query)

    def test_sets_user_id_tag(self, recorder: SpanRecorder) -> None:
        with search_span("query", "user_xyz"):
            pass
        assert recorder.first("therightspot.search").tags["search.user_id"] == "user_xyz"

    def test_inner_spans_captured(self, recorder: SpanRecorder) -> None:
        with search_span("query", "user_abc"):
            with db_span("therightspot.score_venues", "venue_signals"):
                pass
            with ai_span("therightspot.parse_intent"):
                pass
        assert recorder.has("therightspot.search")
        assert recorder.has("therightspot.score_venues")
        assert recorder.has("therightspot.parse_intent")
        assert len(recorder) == 3


# ─── parse_intent integration ─────────────────────────────────────────────────

class TestParseIntentTracing:
    @pytest.mark.asyncio
    async def test_parse_intent_emits_span_on_cache_miss(
        self, recorder: SpanRecorder, birthday_intent: VenueIntent
    ) -> None:
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=birthday_intent.model_dump_json())]
        mock_response.usage = MagicMock(input_tokens=120, output_tokens=80)

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()

        with patch("backend.agents.orchestrator._cache", mock_cache), \
             patch("backend.agents.orchestrator._client") as mock_client:
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            from backend.agents.orchestrator import parse_intent
            result = await parse_intent("birthday dinner for 8 in NYC")

        s = recorder.first("therightspot.parse_intent")
        assert s is not None
        assert s.tags["cache.hit"] is False
        assert s.tags["tokens.input"] == 120
        assert s.tags["tokens.output"] == 80
        assert s.tags["intent.city"] == "New York City"

    @pytest.mark.asyncio
    async def test_parse_intent_emits_cache_hit_span(
        self, recorder: SpanRecorder, birthday_intent: VenueIntent
    ) -> None:
        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=birthday_intent.model_dump_json())

        with patch("backend.agents.orchestrator._cache", mock_cache):
            from backend.agents.orchestrator import parse_intent
            await parse_intent("birthday dinner")

        s = recorder.first("therightspot.parse_intent")
        assert s is not None
        assert s.tags["cache.hit"] is True
        # No API call → no token tags
        assert "tokens.input" not in s.tags

    @pytest.mark.asyncio
    async def test_parse_intent_marks_error_span_on_exception(
        self, recorder: SpanRecorder
    ) -> None:
        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=None)

        bad_response = MagicMock()
        bad_response.content = [MagicMock(text="not json at all")]
        bad_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch("backend.agents.orchestrator._cache", mock_cache), \
             patch("backend.agents.orchestrator._client") as mock_client:
            mock_client.messages.create = AsyncMock(return_value=bad_response)

            from backend.agents.orchestrator import parse_intent
            with pytest.raises(Exception):
                await parse_intent("ambiguous query")

        s = recorder.first("therightspot.parse_intent")
        assert s is not None
        assert s.error == 1


# ─── synthesize_venue_intelligence tracing ────────────────────────────────────

class TestSynthesisTracing:
    @pytest.mark.asyncio
    async def test_synthesis_emits_span(
        self,
        recorder: SpanRecorder,
        birthday_intent: VenueIntent,
        scored_venue: ScoredVenue,
    ) -> None:
        intel = VenueIntelligence(
            why_card="Great fit.",
            scenario="A lovely evening.",
            sensitivity_bars={"ambiance": 90, "privacy": 95, "service": 88, "value": 72, "occasion_fit": 93},
            live_signal=None,
            suggestions=["Q1?", "Q2?", "Q3?", "Q4?"],
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=intel.model_dump_json())]
        mock_response.usage = MagicMock(input_tokens=200, output_tokens=300)

        with patch("backend.agents.orchestrator._client") as mock_client:
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            from backend.agents.orchestrator import synthesize_venue_intelligence
            result = await synthesize_venue_intelligence(scored_venue, birthday_intent)

        s = recorder.first("therightspot.synthesize")
        assert s is not None
        assert s.tags["venue_id"] == "locanda_verde_new_york_city"
        assert s.tags["venue_name"] == "Locanda Verde"
        assert s.tags["tokens.input"] == 200
        assert s.tags["tokens.output"] == 300
        assert isinstance(result, VenueIntelligence)


# ─── extract_signals tracing ──────────────────────────────────────────────────

class TestExtractSignalsTracing:
    @pytest.mark.asyncio
    async def test_extract_signals_emits_span(self, recorder: SpanRecorder) -> None:
        from backend.models.models import ExtractedSignals, RawVenueResult
        from backend.agents.scraper_agent import _call_with_retry

        signals = ExtractedSignals(
            noise_level="quiet",
            has_private_room=True,
            max_group_size=20,
            booking_difficulty="hard",
            special_occasion_score=85,
            birthday_mentions=10,
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=signals.model_dump_json())]
        mock_response.usage = MagicMock(input_tokens=50, output_tokens=30)

        raw = RawVenueResult(
            name="Locanda Verde",
            snippet="Quiet Italian restaurant with private dining rooms.",
        )

        with patch("backend.agents.scraper_agent._client") as mock_client:
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            result = await _call_with_retry(raw)

        s = recorder.first("therightspot.extract_signals")
        assert s is not None
        assert s.tags["venue_name"] == "Locanda Verde"
        assert s.tags["tokens.input"] == 50
        assert s.tags["attempts"] == 1

    @pytest.mark.asyncio
    async def test_extract_signals_no_span_for_empty_snippet(
        self, recorder: SpanRecorder
    ) -> None:
        from backend.models.models import RawVenueResult
        from backend.agents.scraper_agent import _call_with_retry

        raw = RawVenueResult(name="Ghost Venue", snippet="")
        result = await _call_with_retry(raw)

        assert result is None
        assert not recorder.has("therightspot.extract_signals")

    @pytest.mark.asyncio
    async def test_extract_signals_rate_limit_sets_tag(
        self, recorder: SpanRecorder
    ) -> None:
        import anthropic
        from backend.models.models import RawVenueResult
        from backend.agents.scraper_agent import _call_with_retry

        raw = RawVenueResult(
            name="Busy Venue",
            snippet="Great restaurant.",
        )

        # Rate-limited on all attempts
        with patch("backend.agents.scraper_agent._client") as mock_client, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client.messages.create = AsyncMock(
                side_effect=anthropic.RateLimitError(
                    message="rate limited", response=MagicMock(status_code=429), body={}
                )
            )
            result = await _call_with_retry(raw, max_attempts=2)

        s = recorder.first("therightspot.extract_signals")
        assert s is not None
        assert s.tags.get("rate_limited") is True
        assert result is None


# ─── Google Maps client tracing ───────────────────────────────────────────────

class TestGoogleMapsTracing:
    @pytest.mark.asyncio
    async def test_geocode_emits_span(self, recorder: SpanRecorder) -> None:
        from backend.integrations.google_maps_client import GoogleMapsClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "results": [{
                "geometry": {"location": {"lat": 40.72, "lng": -74.01}},
                "formatted_address": "123 Main St",
                "place_id": "ChIJ_abc123",
            }]
        })

        client = GoogleMapsClient.__new__(GoogleMapsClient)
        client._geocoding = AsyncMock()
        client._geocoding.get = AsyncMock(return_value=mock_resp)
        client._places = AsyncMock()

        result = await client.geocode("123 Main St, New York")

        s = recorder.first("therightspot.google_maps.geocode")
        assert s is not None
        assert s.tags["geocode.found"] is True
        assert s.tags["http.status_code"] == 200
        assert result is not None

    @pytest.mark.asyncio
    async def test_geocode_not_found_tags(self, recorder: SpanRecorder) -> None:
        from backend.integrations.google_maps_client import GoogleMapsClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"results": []})

        client = GoogleMapsClient.__new__(GoogleMapsClient)
        client._geocoding = AsyncMock()
        client._geocoding.get = AsyncMock(return_value=mock_resp)
        client._places = AsyncMock()

        result = await client.geocode("nonexistent address")

        s = recorder.first("therightspot.google_maps.geocode")
        assert s.tags["geocode.found"] is False
        assert result is None

    @pytest.mark.asyncio
    async def test_get_place_details_emits_span(self, recorder: SpanRecorder) -> None:
        from backend.integrations.google_maps_client import GoogleMapsClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "id": "ChIJ_test",
            "displayName": {"text": "Locanda Verde"},
            "formattedAddress": "377 Greenwich St",
            "rating": 4.6,
            "userRatingCount": 1200,
            "location": {"latitude": 40.72, "longitude": -74.01},
        })

        client = GoogleMapsClient.__new__(GoogleMapsClient)
        client._places = AsyncMock()
        client._places.get = AsyncMock(return_value=mock_resp)
        client._geocoding = AsyncMock()

        result = await client.get_place_details("ChIJ_test")

        s = recorder.first("therightspot.google_maps.details")
        assert s is not None
        assert s.tags["place_id"] == "ChIJ_test"
        assert s.tags["place.found"] is True
        assert s.tags["http.status_code"] == 200

    @pytest.mark.asyncio
    async def test_get_place_details_404_tags(self, recorder: SpanRecorder) -> None:
        from backend.integrations.google_maps_client import GoogleMapsClient

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        client = GoogleMapsClient.__new__(GoogleMapsClient)
        client._places = AsyncMock()
        client._places.get = AsyncMock(return_value=mock_resp)
        client._geocoding = AsyncMock()

        result = await client.get_place_details("ChIJ_missing")

        s = recorder.first("therightspot.google_maps.details")
        assert s.tags["place.found"] is False
        assert result is None


# ─── Nimble SERP scraper tracing ──────────────────────────────────────────────

class TestNimbleScrapeTracing:
    @pytest.mark.asyncio
    async def test_nimble_maps_emits_span(self, recorder: SpanRecorder) -> None:
        from backend.agents.scraper_agent import ScraperAgent

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "local_results": [
                {
                    "title": "Locanda Verde",
                    "place_id": "ChIJ_abc",
                    "address": "377 Greenwich St",
                    "gps_coordinates": {"latitude": 40.72, "longitude": -74.01},
                }
            ]
        })

        agent = ScraperAgent.__new__(ScraperAgent)
        agent._http = AsyncMock()
        agent._http.post = AsyncMock(return_value=mock_resp)

        results = await agent._nimble_maps_search("italian restaurant NYC birthday")

        s = recorder.first("therightspot.nimble_maps")
        assert s is not None
        assert s.tags["http.service"] == "nimble"
        assert s.tags["search.engine"] == "google_maps"
        assert s.tags["results.count"] == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_nimble_serp_emits_span(self, recorder: SpanRecorder) -> None:
        from backend.agents.scraper_agent import ScraperAgent

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "organic_results": [
                {"title": "Best Italian NYC", "link": "https://example.com", "snippet": "Great place"}
            ]
        })

        agent = ScraperAgent.__new__(ScraperAgent)
        agent._http = AsyncMock()
        agent._http.post = AsyncMock(return_value=mock_resp)

        results = await agent._nimble_serp_search("best italian restaurant NYC")

        s = recorder.first("therightspot.nimble_serp")
        assert s is not None
        assert s.tags["http.service"] == "nimble"
        assert s.tags["search.engine"] == "google"
        assert s.tags["results.count"] == 1

    @pytest.mark.asyncio
    async def test_nimble_error_sets_error_tag(self, recorder: SpanRecorder) -> None:
        from backend.agents.scraper_agent import ScraperAgent

        agent = ScraperAgent.__new__(ScraperAgent)
        agent._http = AsyncMock()
        agent._http.post = AsyncMock(side_effect=Exception("connection refused"))

        results = await agent._nimble_maps_search("query")

        s = recorder.first("therightspot.nimble_maps")
        assert s is not None
        assert s.tags.get("error") is True
        assert results == []


# ─── db_span on ClickHouse operations ────────────────────────────────────────

class TestClickHouseTracing:
    def test_score_venues_span_metadata(self, recorder: SpanRecorder) -> None:
        with db_span("therightspot.score_venues", "venue_signals", city="NYC") as span:
            span.set_tag("db.rows_returned", 12)

        s = recorder.first("therightspot.score_venues")
        assert s.tags["db.type"] == "clickhouse"
        assert s.tags["db.table"] == "venue_signals"
        assert s.tags["city"] == "NYC"
        assert s.tags["db.rows_returned"] == 12

    def test_upsert_span_metadata(self, recorder: SpanRecorder) -> None:
        with db_span("therightspot.upsert_venues", "venue_signals", row_count=5):
            pass

        s = recorder.first("therightspot.upsert_venues")
        assert s.tags["db.table"] == "venue_signals"
        assert s.tags["row_count"] == 5


# ─── End-to-end span tree shape ───────────────────────────────────────────────

class TestSpanTreeShape:
    """Verify that a search request produces the expected span hierarchy."""

    @pytest.mark.asyncio
    async def test_full_search_span_names_present(
        self,
        recorder: SpanRecorder,
        birthday_intent: VenueIntent,
        scored_venue: ScoredVenue,
    ) -> None:
        """Simulate the orchestration span tree without real I/O."""
        intel = VenueIntelligence(
            why_card="Great fit.",
            scenario="Great evening.",
            sensitivity_bars={"ambiance": 90, "privacy": 95, "service": 88, "value": 72, "occasion_fit": 93},
            live_signal=None,
            suggestions=["Q1?", "Q2?", "Q3?", "Q4?"],
        )

        with search_span("birthday dinner for 8 in NYC", "user_test") as root:
            root.set_tag("search.venues_scored", 10)

            with db_span("therightspot.cache_check", "venue_signals", city="NYC"):
                pass

            with ai_span("therightspot.parse_intent", query_length=30) as span:
                span.set_tag("cache.hit", False)
                span.set_tag("intent.city", "New York City")

            with db_span("therightspot.score_venues", "venue_signals") as span:
                span.set_tag("db.rows_returned", 10)

            with ai_span("therightspot.synthesize", venue_id="v1"):
                pass

        expected_spans = [
            "therightspot.search",
            "therightspot.cache_check",
            "therightspot.parse_intent",
            "therightspot.score_venues",
            "therightspot.synthesize",
        ]
        for name in expected_spans:
            assert recorder.has(name), f"Missing span: {name}"

        assert len(recorder) == len(expected_spans)
        root_span = recorder.first("therightspot.search")
        assert root_span.tags["search.venues_scored"] == 10
        assert root_span.error == 0
