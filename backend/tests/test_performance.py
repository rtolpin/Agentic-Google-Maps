"""
Performance tests for The Right Spot AI agents.

What we measure:
  - parse_intent: cache miss vs cache hit latency ratio
  - synthesize_venue_intelligence: semaphore-bounded concurrency throughput
  - ScraperAgent: parallel vs sequential signal extraction speedup
  - orchestrate: end-to-end latency within an acceptable envelope
  - _apply_personalization: O(n) scaling
  - ClickHouseClient.upsert_venue_signals: batch serialization throughput
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..db.clickhouse import ClickHouseClient
from ..models.models import (
    ExtractedSignals,
    NoiseLevel,
    RawVenueResult,
    ScoredVenue,
    UserPreferences,
    VenueIntelligence,
    VenueIntent,
    VenueSignal,
)
from ..agents.orchestrator import _apply_personalization, parse_intent, synthesize_venue_intelligence
from ..agents.scraper_agent import ScraperAgent, _call_with_retry


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_scored_venue(i: int, score: float = 70.0) -> ScoredVenue:
    return ScoredVenue(
        venue_id=f"venue_{i}",
        name=f"Venue {i}",
        city="New York City",
        neighborhood="Tribeca" if i % 2 == 0 else "Soho",
        cuisine="italian" if i % 3 == 0 else "french",
        price_per_head=80 + i,
        has_private_room=(i % 2 == 0),
        max_group_size=10,
        noise_level="quiet" if i % 2 == 0 else "moderate",
        birthday_score=60 + (i % 40),
        match_score=score,
    )


def _make_raw_venue(i: int) -> RawVenueResult:
    return RawVenueResult(
        name=f"Restaurant {i}",
        url=f"https://restaurant{i}.com",
        snippet=f"Great Italian restaurant {i} in NYC known for birthday celebrations and private dining rooms. Excellent service.",
    )


def _make_venue_signal(i: int) -> VenueSignal:
    return VenueSignal(
        venue_id=f"venue_{i}",
        name=f"Restaurant {i}",
        city="New York City",
        noise_level=NoiseLevel.QUIET,
        birthday_score=75,
        price_per_head=90,
    )


def _mock_anthropic_fast() -> MagicMock:
    """AsyncAnthropic mock with a tiny simulated latency."""
    intel = VenueIntelligence(
        why_card="Great fit.",
        scenario="You arrive at 7pm...",
        sensitivity_bars={"ambiance": 90, "privacy": 95, "service": 88, "value": 72, "occasion_fit": 93},
        suggestions=["Q1?", "Q2?", "Q3?", "Q4?"],
    )
    signals = ExtractedSignals(special_occasion_score=80, noise_level=NoiseLevel.QUIET)

    client = MagicMock()

    async def fast_create(**_):
        await asyncio.sleep(0.01)  # 10ms simulated API latency
        # Return the right shape depending on which prompt is in use
        return MagicMock(content=[MagicMock(text=intel.model_dump_json())])

    client.messages.create = fast_create
    return client


# ─── Cache hit vs miss ────────────────────────────────────────────────────────

class TestParseIntentLatency:
    @pytest.mark.asyncio
    async def test_cache_hit_is_at_least_5x_faster_than_miss(self, birthday_intent):
        mock_client = _mock_anthropic_fast()
        mock_client.messages.create = AsyncMock(
            return_value=MagicMock(
                content=[MagicMock(text=birthday_intent.model_dump_json())]
            )
        )

        miss_redis = AsyncMock()
        miss_redis.get = AsyncMock(return_value=None)
        miss_redis.set = AsyncMock()

        hit_redis = AsyncMock()
        hit_redis.get = AsyncMock(return_value=birthday_intent.model_dump_json())

        with patch("backend.agents.orchestrator._client", mock_client):
            # Cache miss
            with patch("backend.agents.orchestrator._cache", miss_redis):
                t0 = time.perf_counter()
                await parse_intent("birthday dinner Italian NYC 8 people quiet")
                miss_time = time.perf_counter() - t0

            # Cache hit
            with patch("backend.agents.orchestrator._cache", hit_redis):
                t0 = time.perf_counter()
                await parse_intent("birthday dinner Italian NYC 8 people quiet")
                hit_time = time.perf_counter() - t0

        assert hit_time < miss_time, "Cache hit should be faster than cache miss"
        ratio = miss_time / max(hit_time, 1e-9)
        assert ratio > 5, f"Expected >5x speedup from cache; got {ratio:.1f}x"

    @pytest.mark.asyncio
    async def test_cache_key_is_deterministic_across_calls(self, birthday_intent):
        from ..agents.orchestrator import _cache_key
        keys = {_cache_key("intent", "birthday dinner NYC") for _ in range(100)}
        assert len(keys) == 1, "Cache key must be stable (not random)"


# ─── Semaphore concurrency ────────────────────────────────────────────────────

class TestSynthesisConcurrency:
    @pytest.mark.asyncio
    async def test_6_synthesis_calls_run_within_2_batches(self, birthday_intent):
        """With semaphore=3 and 10ms latency, 6 calls should take ~20ms not ~60ms."""
        call_log: list[tuple[float, float]] = []
        intel = VenueIntelligence(
            why_card="Great.",
            scenario="Arrive at 7pm.",
            sensitivity_bars={"ambiance": 90, "privacy": 95, "service": 88, "value": 72, "occasion_fit": 93},
            suggestions=["Q1?", "Q2?", "Q3?", "Q4?"],
        )

        async def timed_create(**_):
            start = time.perf_counter()
            await asyncio.sleep(0.02)  # 20ms
            end = time.perf_counter()
            call_log.append((start, end))
            return MagicMock(content=[MagicMock(text=intel.model_dump_json())])

        mock_client = MagicMock()
        mock_client.messages.create = timed_create

        venues = [_make_scored_venue(i) for i in range(6)]

        with patch("backend.agents.orchestrator._client", mock_client):
            t_start = time.perf_counter()
            await asyncio.gather(
                *[synthesize_venue_intelligence(v, birthday_intent) for v in venues]
            )
            total = time.perf_counter() - t_start

        # With sem=3 and 20ms each: should take ~40ms (2 batches of 3), not 120ms
        assert total < 0.09, f"Expected <90ms wall time, got {total*1000:.0f}ms"
        assert len(call_log) == 6

    @pytest.mark.asyncio
    async def test_concurrent_max_never_exceeds_semaphore_limit(self, birthday_intent):
        active_count = 0
        peak_concurrency = 0
        intel = VenueIntelligence(
            why_card="Fit.",
            scenario="Scenario.",
            sensitivity_bars={"ambiance": 90, "privacy": 95, "service": 88, "value": 72, "occasion_fit": 93},
            suggestions=["Q1?", "Q2?", "Q3?", "Q4?"],
        )

        async def counted_create(**_):
            nonlocal active_count, peak_concurrency
            active_count += 1
            peak_concurrency = max(peak_concurrency, active_count)
            await asyncio.sleep(0.01)
            active_count -= 1
            return MagicMock(content=[MagicMock(text=intel.model_dump_json())])

        mock_client = MagicMock()
        mock_client.messages.create = counted_create

        venues = [_make_scored_venue(i) for i in range(9)]

        with patch("backend.agents.orchestrator._client", mock_client):
            await asyncio.gather(
                *[synthesize_venue_intelligence(v, birthday_intent) for v in venues]
            )

        assert peak_concurrency <= 3, (
            f"Semaphore limit is 3, but peak concurrency was {peak_concurrency}"
        )


# ─── Scraper parallel speedup ─────────────────────────────────────────────────

class TestScraperParallelism:
    @pytest.mark.asyncio
    async def test_parallel_signal_extraction_faster_than_sequential(
        self, birthday_intent
    ):
        n_venues = 5
        signals = ExtractedSignals(special_occasion_score=80, noise_level=NoiseLevel.QUIET)

        async def slow_create(**_):
            await asyncio.sleep(0.02)  # 20ms each
            return MagicMock(content=[MagicMock(text=signals.model_dump_json())])

        mock_client = MagicMock()
        mock_client.messages.create = slow_create

        raw_venues = [_make_raw_venue(i) for i in range(n_venues)]

        # Sequential baseline
        t0 = time.perf_counter()
        with patch("backend.agents.scraper_agent._client", mock_client):
            for v in raw_venues:
                await _call_with_retry(v)
        sequential_time = time.perf_counter() - t0

        # Parallel (as ScraperAgent does it)
        t0 = time.perf_counter()
        with patch("backend.agents.scraper_agent._client", mock_client):
            await asyncio.gather(*[_call_with_retry(v) for v in raw_venues])
        parallel_time = time.perf_counter() - t0

        speedup = sequential_time / max(parallel_time, 1e-9)
        assert speedup > 2.0, (
            f"Expected >2x parallel speedup over sequential; got {speedup:.1f}x"
        )

    @pytest.mark.asyncio
    async def test_scraper_claude_calls_bounded_by_semaphore(self, birthday_intent):
        active = 0
        peak = 0
        signals = ExtractedSignals(special_occasion_score=70)

        async def counted(**_):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return MagicMock(content=[MagicMock(text=signals.model_dump_json())])

        mock_client = MagicMock()
        mock_client.messages.create = counted

        raw_venues = [_make_raw_venue(i) for i in range(12)]

        with patch("backend.agents.scraper_agent._client", mock_client):
            await asyncio.gather(*[_call_with_retry(v) for v in raw_venues])

        assert peak <= 5, f"Scraper semaphore is 5, but peak was {peak}"


# ─── Personalization scaling ──────────────────────────────────────────────────

class TestPersonalizationScaling:
    def test_linear_scaling_100_vs_1000_venues(self):
        prefs = UserPreferences(
            prefers_quiet=True,
            preferred_cuisines=["italian"],
            preferred_neighborhoods=["Tribeca"],
            prefers_private_room=True,
        )
        venues_100 = [_make_scored_venue(i) for i in range(100)]
        venues_1000 = [_make_scored_venue(i) for i in range(1000)]

        t0 = time.perf_counter()
        _apply_personalization(venues_100, prefs)
        t100 = time.perf_counter() - t0

        t0 = time.perf_counter()
        _apply_personalization(venues_1000, prefs)
        t1000 = time.perf_counter() - t0

        # Sorting is O(n log n) — allow up to 15x for 10x more items
        ratio = t1000 / max(t100, 1e-9)
        assert ratio < 15, f"Personalization not scaling linearly: {ratio:.1f}x for 10x data"

    def test_result_is_sorted_descending(self):
        prefs = UserPreferences()
        venues = [_make_scored_venue(i, score=float(i * 10)) for i in range(10)]
        result = _apply_personalization(venues, prefs)
        scores = [v.match_score for v in result]
        assert scores == sorted(scores, reverse=True)


# ─── ClickHouse batch serialization ───────────────────────────────────────────

class TestClickHouseBatchThroughput:
    def test_upsert_100_venues_under_100ms(self):
        mock_inner = MagicMock()
        mock_inner.insert = MagicMock()

        venues = [_make_venue_signal(i).model_dump() for i in range(100)]

        with patch("clickhouse_connect.get_client", return_value=mock_inner):
            ch = ClickHouseClient()
            t0 = time.perf_counter()
            ch.upsert_venue_signals(venues, "New York City")
            elapsed = time.perf_counter() - t0

        assert elapsed < 0.1, (
            f"Serializing 100 venues took {elapsed*1000:.0f}ms — expected <100ms"
        )

    def test_to_ch_row_produces_correct_column_count(self):
        signal = _make_venue_signal(1)
        row = signal.to_ch_row(datetime.utcnow())
        assert len(row) == len(VenueSignal.CH_COLUMNS)

    def test_enum_encoding_is_integer_not_string(self):
        signal = _make_venue_signal(1)
        row = signal.to_ch_row(datetime.utcnow())
        noise_idx = VenueSignal.CH_COLUMNS.index("noise_level")
        assert isinstance(row[noise_idx], int), "Enum must be stored as int in ClickHouse"
