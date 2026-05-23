"""
Unit tests for Pydantic models — validation, serialization, and helpers.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from ..models.models import (
    BookingDifficulty,
    ExtractedSignals,
    FeedbackSignal,
    NoiseLevel,
    NoisePreference,
    PriceBand,
    ScoredVenue,
    UserPreferences,
    VenueIntelligence,
    VenueIntent,
    VenueSignal,
    WifiQuality,
)


# ─── Enum helpers ─────────────────────────────────────────────────────────────

class TestNoiseLevelEnum:
    def test_ch_int_values_are_ordered(self):
        assert NoiseLevel.VERY_QUIET.ch_int < NoiseLevel.QUIET.ch_int
        assert NoiseLevel.QUIET.ch_int < NoiseLevel.MODERATE.ch_int
        assert NoiseLevel.MODERATE.ch_int < NoiseLevel.LOUD.ch_int
        assert NoiseLevel.LOUD.ch_int < NoiseLevel.VERY_LOUD.ch_int

    def test_roundtrip_through_ch_int(self):
        for level in NoiseLevel:
            assert NoiseLevel.from_ch_int(level.ch_int) == level

    def test_unknown_ch_int_falls_back_to_moderate(self):
        assert NoiseLevel.from_ch_int(99) == NoiseLevel.MODERATE


class TestPriceBandEnum:
    def test_price_ranges_are_ordered(self):
        bands = [PriceBand.BUDGET, PriceBand.MID, PriceBand.UPSCALE, PriceBand.LUXURY]
        mins = [b.price_range[0] for b in bands]
        assert mins == sorted(mins)

    def test_no_gap_or_overlap_between_adjacent_bands(self):
        budget_max = PriceBand.BUDGET.price_range[1]
        mid_min = PriceBand.MID.price_range[0]
        assert budget_max == mid_min


# ─── VenueIntent ──────────────────────────────────────────────────────────────

class TestVenueIntent:
    def test_valid_creation(self):
        intent = VenueIntent(city="New York City", occasion="birthday_dinner", group_size=8)
        assert intent.city == "New York City"
        assert intent.group_size == 8

    def test_city_strips_whitespace(self):
        intent = VenueIntent(city="  Tokyo  ")
        assert intent.city == "Tokyo"

    def test_empty_city_raises(self):
        with pytest.raises(ValidationError):
            VenueIntent(city="   ")

    def test_group_size_bounds(self):
        with pytest.raises(ValidationError):
            VenueIntent(city="NYC", group_size=0)
        with pytest.raises(ValidationError):
            VenueIntent(city="NYC", group_size=501)

    def test_price_range_without_band(self):
        intent = VenueIntent(city="NYC")
        assert intent.price_range == (0, 999)

    def test_price_range_with_band(self):
        intent = VenueIntent(city="NYC", price_band=PriceBand.LUXURY)
        lo, hi = intent.price_range
        assert lo >= 100  # luxury floor

    def test_noise_sql_value_quiet(self):
        intent = VenueIntent(city="NYC", noise_preference=NoisePreference.QUIET)
        assert intent.noise_sql_value == "quiet"

    def test_noise_sql_value_default(self):
        intent = VenueIntent(city="NYC")
        assert intent.noise_sql_value == "moderate"

    def test_to_score_params_keys(self):
        intent = VenueIntent(
            city="NYC",
            cuisine="italian",
            group_size=4,
            needs_private_room=True,
            noise_preference=NoisePreference.QUIET,
            price_band=PriceBand.MID,
            occasion="business_lunch",
        )
        params = intent.to_score_params()
        required = {"city", "cuisine", "group_size", "needs_private_room",
                    "noise_pref", "price_min", "price_max", "occasion"}
        assert required.issubset(params.keys())

    def test_to_score_params_price_bounds(self):
        intent = VenueIntent(city="NYC", price_band=PriceBand.MID)
        params = intent.to_score_params()
        assert params["price_min"] == 40
        assert params["price_max"] == 80


# ─── ExtractedSignals ─────────────────────────────────────────────────────────

class TestExtractedSignals:
    def test_key_quotes_capped_at_3(self):
        s = ExtractedSignals(key_quotes=["a", "b", "c", "d", "e"])
        assert len(s.key_quotes) == 3

    def test_defaults_are_safe(self):
        s = ExtractedSignals()
        assert s.special_occasion_score == 0
        assert s.birthday_mentions == 0
        assert s.noise_level is None

    def test_score_bounds(self):
        with pytest.raises(ValidationError):
            ExtractedSignals(special_occasion_score=101)
        with pytest.raises(ValidationError):
            ExtractedSignals(special_occasion_score=-1)


# ─── VenueSignal ──────────────────────────────────────────────────────────────

class TestVenueSignal:
    def test_ch_columns_matches_to_ch_row_length(self, sample_venue_signal):
        now = datetime.utcnow()
        row = sample_venue_signal.to_ch_row(now)
        assert len(row) == len(VenueSignal.CH_COLUMNS)

    def test_to_ch_row_encodes_enums_as_ints(self, sample_venue_signal):
        row = sample_venue_signal.to_ch_row(datetime.utcnow())
        noise_idx = VenueSignal.CH_COLUMNS.index("noise_level")
        assert isinstance(row[noise_idx], int)

    def test_to_ch_row_bool_columns_are_ints(self, sample_venue_signal):
        row = sample_venue_signal.to_ch_row(datetime.utcnow())
        for col in ("has_private_room", "dog_friendly", "outdoor_seating"):
            idx = VenueSignal.CH_COLUMNS.index(col)
            assert row[idx] in (0, 1)

    def test_signal_age_hrs_is_zero_placeholder(self, sample_venue_signal):
        row = sample_venue_signal.to_ch_row(datetime.utcnow())
        age_idx = VenueSignal.CH_COLUMNS.index("signal_age_hrs")
        assert row[age_idx] == 0


# ─── ScoredVenue ──────────────────────────────────────────────────────────────

class TestScoredVenue:
    def test_from_ch_row_all_fields(self):
        now = datetime(2026, 5, 23, 10, 0, 0)
        row = (
            "venue_id_1", "Test Venue", "NYC", "Tribeca", "italian",
            "ChIJTEST", "123 Test St", 40.72, -74.01,
            85, 1, 12, "quiet", 75, ["Great food"], now, 91.5,
        )
        venue = ScoredVenue.from_ch_row(row)
        assert venue.venue_id == "venue_id_1"
        assert venue.has_private_room is True
        assert venue.match_score == 91.5
        assert venue.noise_level == "quiet"
        assert venue.place_id == "ChIJTEST"

    def test_from_ch_row_null_scraped_at(self):
        row = ("v1", "Venue", "NYC", "", "", "", "", None, None, 50, 0, 6, "moderate", 0, [], None, 55.0)
        venue = ScoredVenue.from_ch_row(row)
        assert venue.scraped_at is None


# ─── VenueIntelligence ────────────────────────────────────────────────────────

class TestVenueIntelligence:
    def test_sensitivity_bars_clamped(self):
        intel = VenueIntelligence(
            why_card="Great fit.",
            scenario="You arrive at 7pm...",
            sensitivity_bars={"ambiance": 150, "privacy": -10, "service": 80},
            suggestions=["a", "b", "c", "d"],
        )
        assert intel.sensitivity_bars["ambiance"] == 100
        assert intel.sensitivity_bars["privacy"] == 0

    def test_suggestions_capped_at_4(self):
        intel = VenueIntelligence(
            why_card="Great fit.",
            scenario="Scenario.",
            sensitivity_bars={},
            suggestions=["q1", "q2", "q3", "q4", "q5"],
        )
        assert len(intel.suggestions) == 4


# ─── FeedbackSignal ───────────────────────────────────────────────────────────

class TestFeedbackSignal:
    def test_valid_feedback_values(self):
        for v in (-1, 0, 1):
            f = FeedbackSignal(user_id="u1", venue_id="v1", query="q", feedback=v)
            assert f.feedback == v

    def test_invalid_feedback_raises(self):
        with pytest.raises(ValidationError):
            FeedbackSignal(user_id="u1", venue_id="v1", query="q", feedback=2)


# ─── UserPreferences ──────────────────────────────────────────────────────────

class TestUserPreferences:
    def test_defaults(self):
        p = UserPreferences()
        assert p.prefers_quiet is False
        assert p.preferred_cuisines == []

    def test_price_ceiling_must_be_non_negative(self):
        with pytest.raises(ValidationError):
            UserPreferences(price_ceiling=-5)
