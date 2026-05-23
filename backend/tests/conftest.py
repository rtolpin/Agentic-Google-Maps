"""
Shared pytest fixtures for The Right Spot test suite.
All external I/O (Anthropic, ClickHouse, Redis, httpx) is mocked here.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..models.models import (
    BookingDifficulty,
    ExtractedSignals,
    NoiseLevel,
    PriceBand,
    RawVenueResult,
    ScoredVenue,
    UserPreferences,
    VenueIntelligence,
    VenueIntent,
    VenueSignal,
    WifiQuality,
)


# ─── Domain fixtures ──────────────────────────────────────────────────────────

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
        dietary_restrictions=["vegetarian"],
    )


@pytest.fixture
def business_intent() -> VenueIntent:
    return VenueIntent(
        occasion="business_lunch",
        group_size=4,
        city="Tokyo",
        needs_private_room=True,
        price_band=PriceBand.UPSCALE,
    )


@pytest.fixture
def sample_scored_venue() -> ScoredVenue:
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
        key_quotes=["Perfect for celebrations", "Private dining room available"],
        scraped_at="2026-05-23T10:00:00",
        match_score=87.5,
    )


@pytest.fixture
def sample_venue_signal() -> VenueSignal:
    return VenueSignal(
        venue_id="locanda_verde_new_york_city",
        name="Locanda Verde",
        city="New York City",
        neighborhood="Tribeca",
        cuisine="italian",
        noise_level=NoiseLevel.QUIET,
        has_private_room=True,
        max_group_size=20,
        birthday_score=82,
        price_per_head=95,
        booking_difficulty=BookingDifficulty.HARD,
        special_occasion_score=88,
        birthday_mentions=15,
        key_quotes=["Perfect for celebrations"],
        scraped_at=datetime(2026, 5, 23, 10, 0, 0),
    )


@pytest.fixture
def sample_intelligence() -> VenueIntelligence:
    return VenueIntelligence(
        why_card="Locanda Verde's private dining room seats up to 20 guests with a hushed atmosphere perfectly suited for intimate birthday celebrations. The Italian menu's celebratory prix-fixe menus make large-group coordination effortless.",
        scenario="Your party of eight arrives at 7 PM to a candle-lit private room...",
        sensitivity_bars={"ambiance": 90, "privacy": 95, "service": 88, "value": 72, "occasion_fit": 93},
        live_signal="Books out ~3 weeks ahead for weekend private rooms",
        suggestions=[
            "Do they offer a tasting menu for the whole table?",
            "Can we bring a custom birthday cake?",
            "Is valet parking available nearby?",
            "What is the minimum spend for the private room?",
        ],
    )


@pytest.fixture
def raw_venue() -> RawVenueResult:
    return RawVenueResult(
        name="Locanda Verde",
        url="https://locandaverde.com",
        snippet="Celebrated Italian restaurant in Tribeca known for private dining rooms and special occasion menus. Birthday-friendly with dedicated celebration packages.",
        source="locandaverde.com",
    )


@pytest.fixture
def user_prefs() -> UserPreferences:
    return UserPreferences(
        prefers_quiet=True,
        preferred_neighborhoods=["Tribeca", "West Village"],
        preferred_cuisines=["italian", "french"],
        prefers_private_room=True,
        price_ceiling=120,
    )


# ─── Anthropic mock helpers ───────────────────────────────────────────────────

def _make_anthropic_response(text: str) -> MagicMock:
    """Build a mock that looks like an anthropic.types.Message."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


def mock_intent_response(intent: VenueIntent) -> MagicMock:
    return _make_anthropic_response(intent.model_dump_json())


def mock_intelligence_response(intel: VenueIntelligence) -> MagicMock:
    return _make_anthropic_response(intel.model_dump_json())


def mock_signals_response(signals: ExtractedSignals) -> MagicMock:
    return _make_anthropic_response(signals.model_dump_json())


def mock_guide_response(text: str = "# Guide\n\nSample guide content.") -> MagicMock:
    return _make_anthropic_response(text)


# ─── Async Anthropic client fixture ──────────────────────────────────────────

@pytest.fixture
def async_anthropic_client():
    """Patched AsyncAnthropic that never hits the real API."""
    mock = AsyncMock()
    mock.messages.create = AsyncMock()
    return mock


# ─── Redis mock ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.get_user_prefs = AsyncMock(return_value=None)
    return redis


# ─── ClickHouse mock ──────────────────────────────────────────────────────────

@pytest.fixture
def mock_ch(sample_scored_venue):
    ch = MagicMock()
    ch.get_cached_scores = MagicMock(return_value=[])
    ch.upsert_venue_signals = MagicMock(return_value=None)
    ch.score_venues = MagicMock(return_value=[sample_scored_venue])
    ch.record_session = MagicMock(return_value=None)
    ch.get_city_benchmarks = MagicMock(return_value={})
    ch.get_venue_by_id = MagicMock(return_value={"venue_id": "test", "name": "Test"})
    ch.initialize_schema = MagicMock(return_value=None)
    return ch


# ─── httpx mock ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_http_client():
    http = AsyncMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=None)
    return http


# ─── Event loop fixture ───────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
