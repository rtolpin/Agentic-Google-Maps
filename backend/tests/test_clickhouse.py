"""
Unit tests for ClickHouseClient — schema init, upsert, scoring, caching, benchmarks.
All database calls are mocked so tests run without a live ClickHouse instance.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from ..db.clickhouse import ClickHouseClient, _SCORE_QUERY
from ..models.models import (
    BookingDifficulty,
    CityBenchmark,
    NoiseLevel,
    PriceBand,
    ScoredVenue,
    VenueIntent,
    VenueSignal,
    WifiQuality,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ch_client(mock_ch):
    """Return a ClickHouseClient whose internal client is fully mocked."""
    client = ClickHouseClient.__new__(ClickHouseClient)
    client.client = mock_ch.client = MagicMock()
    return client


@pytest.fixture
def scored_row():
    """A tuple matching ScoredVenue.from_ch_row() expectations."""
    return (
        "venue_id_1",        # venue_id
        "Locanda Verde",     # name
        "New York City",     # city
        "Tribeca",           # neighborhood
        "italian",           # cuisine
        95,                  # price_per_head
        1,                   # has_private_room
        20,                  # max_group_size
        "quiet",             # noise_level
        82,                  # birthday_score
        ["Great birthday"],  # key_quotes
        datetime(2026, 5, 23),  # scraped_at
        91.5,                # match_score
    )


# ─── initialize_schema ────────────────────────────────────────────────────────

class TestInitializeSchema:
    def test_executes_all_ddl_statements(self):
        mock_inner = MagicMock()
        mock_inner.command = MagicMock()

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            ch.initialize_schema()

        # Should call command() at least once per CREATE statement
        assert mock_inner.command.call_count >= 3

    def test_skips_empty_statements(self):
        mock_inner = MagicMock()
        mock_inner.command = MagicMock()

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            ch.initialize_schema()

        for c in mock_inner.command.call_args_list:
            stmt = c[0][0].strip()
            assert len(stmt) > 0


# ─── upsert_venue_signals ─────────────────────────────────────────────────────

class TestUpsertVenueSignals:
    def test_empty_list_does_not_call_insert(self):
        mock_inner = MagicMock()
        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            ch.upsert_venue_signals([], "NYC")
        mock_inner.insert.assert_not_called()

    def test_valid_venues_call_insert_once(self, sample_venue_signal):
        mock_inner = MagicMock()
        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            ch.upsert_venue_signals(
                [sample_venue_signal.model_dump()], "New York City"
            )
        mock_inner.insert.assert_called_once()

    def test_insert_uses_ch_columns(self, sample_venue_signal):
        mock_inner = MagicMock()
        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            ch.upsert_venue_signals(
                [sample_venue_signal.model_dump()], "New York City"
            )
        _, kwargs = mock_inner.insert.call_args
        assert kwargs.get("column_names") == VenueSignal.CH_COLUMNS

    def test_malformed_venue_skipped_silently(self):
        mock_inner = MagicMock()
        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            # city field is required by VenueSignal — missing venue_id is filled from name+city
            ch.upsert_venue_signals([{"name": "Good Venue", "city": "NYC"}], "NYC")
        # Should not raise; malformed ones are skipped


# ─── score_venues ─────────────────────────────────────────────────────────────

class TestScoreVenues:
    def test_returns_scored_venues(self, birthday_intent, scored_row):
        mock_result = MagicMock()
        mock_result.result_rows = [scored_row]
        mock_inner = MagicMock()
        mock_inner.query = MagicMock(return_value=mock_result)

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            results = ch.score_venues(birthday_intent)

        assert len(results) == 1
        assert isinstance(results[0], ScoredVenue)

    def test_score_query_uses_to_score_params(self, birthday_intent):
        mock_result = MagicMock()
        mock_result.result_rows = []
        mock_inner = MagicMock()
        mock_inner.query = MagicMock(return_value=mock_result)

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            ch.score_venues(birthday_intent)

        call_kwargs = mock_inner.query.call_args[1]
        params = call_kwargs.get("parameters", {})
        assert params["city"] == birthday_intent.city
        assert params["occasion"] == birthday_intent.occasion

    def test_empty_result_returns_empty_list(self, birthday_intent):
        mock_result = MagicMock()
        mock_result.result_rows = []
        mock_inner = MagicMock()
        mock_inner.query = MagicMock(return_value=mock_result)

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            results = ch.score_venues(birthday_intent)

        assert results == []


# ─── get_cached_scores ────────────────────────────────────────────────────────

class TestGetCachedScores:
    def test_returns_scored_venues_when_cached(self, scored_row):
        mock_result = MagicMock()
        mock_result.result_rows = [scored_row]
        mock_inner = MagicMock()
        mock_inner.query = MagicMock(return_value=mock_result)

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            results = ch.get_cached_scores("New York City", "italian")

        assert len(results) == 1
        assert results[0].city == "New York City"

    def test_returns_empty_list_on_cache_miss(self):
        mock_result = MagicMock()
        mock_result.result_rows = []
        mock_inner = MagicMock()
        mock_inner.query = MagicMock(return_value=mock_result)

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            results = ch.get_cached_scores("Tokyo", "sushi")

        assert results == []


# ─── get_city_benchmarks ──────────────────────────────────────────────────────

class TestGetCityBenchmarks:
    def test_returns_dict_of_benchmarks(self):
        mock_result = MagicMock()
        mock_result.result_rows = [
            ("New York City", 82.5, 95.0, 0.45, 1200),
            ("Tokyo", 78.0, 60.0, 0.30, 800),
        ]
        mock_inner = MagicMock()
        mock_inner.query = MagicMock(return_value=mock_result)

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            result = ch.get_city_benchmarks(["New York City", "Tokyo"], "birthday_dinner")

        assert "New York City" in result
        assert "Tokyo" in result
        assert isinstance(result["New York City"], CityBenchmark)

    def test_benchmark_values_are_rounded(self):
        mock_result = MagicMock()
        mock_result.result_rows = [("Paris", 77.777, 123.456, 0.3333, 500)]
        mock_inner = MagicMock()
        mock_inner.query = MagicMock(return_value=mock_result)

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            result = ch.get_city_benchmarks(["Paris"], "dinner")

        b = result["Paris"]
        assert b.occasion_score == round(77.777, 1)
        assert b.private_room_rate == round(0.3333, 2)


# ─── SCORE_QUERY structure ────────────────────────────────────────────────────

class TestScoreQueryStructure:
    def test_uses_dateDiff_for_freshness(self):
        assert "dateDiff" in _SCORE_QUERY

    def test_has_final_clause(self):
        assert "FINAL" in _SCORE_QUERY

    def test_has_ttl_filter(self):
        assert "7 DAY" in _SCORE_QUERY

    def test_noise_pref_handles_lively(self):
        assert "lively" in _SCORE_QUERY

    def test_birthday_occasion_uses_birthday_score(self):
        assert "birthday_score" in _SCORE_QUERY
        assert "birthday_dinner" in _SCORE_QUERY
