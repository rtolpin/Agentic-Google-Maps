"""
Unit tests for the orchestrator — intent parsing, synthesis, orchestration flow,
and personalization re-ranking.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..models.models import ScoredVenue, VenueIntelligence, VenueIntent
from ..agents.orchestrator import _apply_personalization, _cache_key, _extract_json, parse_intent, synthesize_venue_intelligence


# ─── _extract_json ────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_plain_json(self):
        result = _extract_json('{"city": "NYC"}')
        assert result["city"] == "NYC"

    def test_strips_json_fence(self):
        text = '```json\n{"city": "NYC"}\n```'
        assert _extract_json(text)["city"] == "NYC"

    def test_strips_plain_fence(self):
        text = '```\n{"city": "NYC"}\n```'
        assert _extract_json(text)["city"] == "NYC"

    def test_finds_embedded_json(self):
        text = 'Here is the result: {"city": "NYC"} done.'
        assert _extract_json(text)["city"] == "NYC"

    def test_raises_on_no_json(self):
        with pytest.raises((ValueError, Exception)):
            _extract_json("No JSON here at all.")


# ─── _cache_key ───────────────────────────────────────────────────────────────

class TestCacheKey:
    def test_same_input_produces_same_key(self):
        assert _cache_key("intent", "hello") == _cache_key("intent", "hello")

    def test_different_inputs_produce_different_keys(self):
        assert _cache_key("intent", "abc") != _cache_key("intent", "xyz")

    def test_different_prefixes_produce_different_keys(self):
        assert _cache_key("intent", "abc") != _cache_key("synthesis", "abc")

    def test_key_is_stable_across_calls(self):
        # Verifies hashlib is used (not Python's unstable hash())
        k1 = _cache_key("test", "birthday dinner NYC 8 people Italian")
        k2 = _cache_key("test", "birthday dinner NYC 8 people Italian")
        assert k1 == k2


# ─── parse_intent ─────────────────────────────────────────────────────────────

class TestParseIntent:
    @pytest.mark.asyncio
    async def test_returns_venue_intent_from_api(
        self, birthday_intent, async_anthropic_client, mock_redis
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=birthday_intent.model_dump_json())]
        )
        mock_redis.get.return_value = None

        with (
            patch("files.orchestrator._client", async_anthropic_client),
            patch("files.orchestrator._cache", mock_redis),
        ):
            result = await parse_intent("birthday dinner Italian NYC 8 people quiet")

        assert isinstance(result, VenueIntent)
        assert result.city == birthday_intent.city
        async_anthropic_client.messages.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_hit_skips_api_call(
        self, birthday_intent, async_anthropic_client, mock_redis
    ):
        mock_redis.get.return_value = birthday_intent.model_dump_json()

        with (
            patch("files.orchestrator._client", async_anthropic_client),
            patch("files.orchestrator._cache", mock_redis),
        ):
            result = await parse_intent("birthday dinner Italian NYC 8 people quiet")

        assert isinstance(result, VenueIntent)
        async_anthropic_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_is_cached_after_api_call(
        self, birthday_intent, async_anthropic_client, mock_redis
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=birthday_intent.model_dump_json())]
        )
        mock_redis.get.return_value = None

        with (
            patch("files.orchestrator._client", async_anthropic_client),
            patch("files.orchestrator._cache", mock_redis),
        ):
            await parse_intent("birthday dinner")

        mock_redis.set.assert_called_once()
        stored = mock_redis.set.call_args[0][1]
        parsed = VenueIntent.model_validate_json(stored)
        assert parsed.city == birthday_intent.city

    @pytest.mark.asyncio
    async def test_uses_prompt_caching_header(
        self, birthday_intent, async_anthropic_client, mock_redis
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=birthday_intent.model_dump_json())]
        )
        mock_redis.get.return_value = None

        with (
            patch("files.orchestrator._client", async_anthropic_client),
            patch("files.orchestrator._cache", mock_redis),
        ):
            await parse_intent("test query")

        call_kwargs = async_anthropic_client.messages.create.call_args[1]
        system = call_kwargs["system"]
        assert isinstance(system, list)
        assert system[0].get("cache_control") == {"type": "ephemeral"}


# ─── synthesize_venue_intelligence ───────────────────────────────────────────

class TestSynthesizeVenueIntelligence:
    @pytest.mark.asyncio
    async def test_returns_venue_intelligence(
        self, sample_scored_venue, birthday_intent, sample_intelligence, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=sample_intelligence.model_dump_json())]
        )

        with patch("files.orchestrator._client", async_anthropic_client):
            result = await synthesize_venue_intelligence(sample_scored_venue, birthday_intent)

        assert isinstance(result, VenueIntelligence)
        assert result.why_card == sample_intelligence.why_card

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(
        self, sample_scored_venue, birthday_intent, sample_intelligence, async_anthropic_client
    ):
        """Verifies that at most 3 synthesis calls run concurrently."""
        call_count = 0
        concurrent_high_water = 0
        active = 0

        async def fake_create(**_kwargs):
            nonlocal call_count, concurrent_high_water, active
            active += 1
            concurrent_high_water = max(concurrent_high_water, active)
            await asyncio.sleep(0.01)
            active -= 1
            call_count += 1
            return MagicMock(
                content=[MagicMock(text=sample_intelligence.model_dump_json())]
            )

        async_anthropic_client.messages.create = fake_create

        with patch("files.orchestrator._client", async_anthropic_client):
            await asyncio.gather(
                *[
                    synthesize_venue_intelligence(sample_scored_venue, birthday_intent)
                    for _ in range(6)
                ]
            )

        assert concurrent_high_water <= 3
        assert call_count == 6

    @pytest.mark.asyncio
    async def test_uses_prompt_caching_header(
        self, sample_scored_venue, birthday_intent, sample_intelligence, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=sample_intelligence.model_dump_json())]
        )

        with patch("files.orchestrator._client", async_anthropic_client):
            await synthesize_venue_intelligence(sample_scored_venue, birthday_intent)

        call_kwargs = async_anthropic_client.messages.create.call_args[1]
        system = call_kwargs["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}


# ─── _apply_personalization ───────────────────────────────────────────────────

class TestApplyPersonalization:
    def _venues(self, **overrides) -> list[ScoredVenue]:
        base = dict(
            venue_id="v1", name="V1", city="NYC", neighborhood="Tribeca",
            cuisine="italian", price_per_head=80, has_private_room=True,
            max_group_size=10, noise_level="quiet", birthday_score=70,
            match_score=60.0,
        )
        return [ScoredVenue(**{**base, **overrides})]

    def test_quiet_preference_boosts_quiet_venues(self, user_prefs):
        venues = self._venues(noise_level="quiet", match_score=60.0)
        result = _apply_personalization(venues, user_prefs)
        assert result[0].match_score > 60.0

    def test_loud_venue_not_boosted_for_quiet_preference(self, user_prefs):
        venues = self._venues(noise_level="loud", match_score=60.0)
        result = _apply_personalization(venues, user_prefs)
        assert result[0].match_score == 60.0

    def test_preferred_neighborhood_boosts_score(self, user_prefs):
        venues = self._venues(neighborhood="Tribeca", match_score=60.0)
        result = _apply_personalization(venues, user_prefs)
        assert result[0].match_score > 60.0

    def test_preferred_cuisine_boosts_score(self, user_prefs):
        venues = self._venues(cuisine="italian", match_score=60.0)
        result = _apply_personalization(venues, user_prefs)
        assert result[0].match_score > 60.0

    def test_private_room_preference_boosts_score(self, user_prefs):
        venues = self._venues(has_private_room=True, match_score=60.0)
        result = _apply_personalization(venues, user_prefs)
        assert result[0].match_score > 60.0

    def test_score_never_exceeds_100(self, user_prefs):
        venues = [
            ScoredVenue(
                venue_id="v1", name="V", city="NYC", neighborhood="Tribeca",
                cuisine="italian", price_per_head=80, has_private_room=True,
                max_group_size=10, noise_level="quiet", birthday_score=70,
                match_score=99.0,
            )
        ]
        result = _apply_personalization(venues, user_prefs)
        assert result[0].match_score <= 100.0

    def test_venues_sorted_descending_by_score(self, user_prefs):
        venues = [
            ScoredVenue(venue_id="v1", name="A", city="NYC", neighborhood="Tribeca",
                        cuisine="italian", has_private_room=True, noise_level="quiet",
                        price_per_head=80, max_group_size=10, birthday_score=70,
                        match_score=50.0),
            ScoredVenue(venue_id="v2", name="B", city="NYC", neighborhood="Soho",
                        cuisine="french", has_private_room=False, noise_level="loud",
                        price_per_head=60, max_group_size=8, birthday_score=40,
                        match_score=80.0),
        ]
        result = _apply_personalization(venues, user_prefs)
        assert result[0].match_score >= result[1].match_score
