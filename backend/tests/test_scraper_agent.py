"""
Unit tests for ScraperAgent — Nimble search, Claude signal extraction,
deduplication, retry logic, and concurrent semaphore enforcement.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..models.models import ExtractedSignals, NoiseLevel, RawVenueResult, VenueIntent
from ..agents.scraper_agent import ScraperAgent, _call_with_retry


# ─── Deduplication ────────────────────────────────────────────────────────────

class TestDeduplicate:
    def test_removes_exact_duplicates(self):
        venues = [
            RawVenueResult(name="Locanda Verde", snippet="Great Italian"),
            RawVenueResult(name="Locanda Verde", snippet="Another mention"),
        ]
        result = ScraperAgent._deduplicate(venues)
        assert len(result) == 1

    def test_normalises_apostrophes_and_spaces(self):
        venues = [
            RawVenueResult(name="L'Artusi", snippet="French"),
            RawVenueResult(name="LArtusi", snippet="French copy"),
        ]
        result = ScraperAgent._deduplicate(venues)
        assert len(result) == 1

    def test_keeps_first_occurrence(self):
        venues = [
            RawVenueResult(name="Primo", snippet="First"),
            RawVenueResult(name="Primo", snippet="Second"),
        ]
        result = ScraperAgent._deduplicate(venues)
        assert result[0].snippet == "First"

    def test_filters_out_very_short_names(self):
        venues = [RawVenueResult(name="AB", snippet="Too short")]
        result = ScraperAgent._deduplicate(venues)
        assert len(result) == 0

    def test_preserves_distinct_venues(self):
        venues = [
            RawVenueResult(name="Locanda Verde", snippet="Italian"),
            RawVenueResult(name="Le Bernardin", snippet="French"),
            RawVenueResult(name="Nobu", snippet="Japanese"),
        ]
        result = ScraperAgent._deduplicate(venues)
        assert len(result) == 3


# ─── _call_with_retry ─────────────────────────────────────────────────────────

class TestCallWithRetry:
    @pytest.mark.asyncio
    async def test_returns_none_for_empty_snippet(self):
        raw = RawVenueResult(name="Test", snippet="")
        result = await _call_with_retry(raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_extracted_signals_on_success(
        self, raw_venue, async_anthropic_client
    ):
        signals = ExtractedSignals(
            noise_level=NoiseLevel.QUIET,
            special_occasion_score=85,
            birthday_mentions=10,
            key_quotes=["Great for birthdays"],
        )
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=signals.model_dump_json())]
        )

        with patch("backend.agents.scraper_agent._client", async_anthropic_client):
            result = await _call_with_retry(raw_venue)

        assert isinstance(result, ExtractedSignals)
        assert result.noise_level == NoiseLevel.QUIET
        assert result.special_occasion_score == 85

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_error(
        self, raw_venue, async_anthropic_client
    ):
        import anthropic

        signals = ExtractedSignals(special_occasion_score=60)
        call_count = 0

        async def flaky_create(**_):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise anthropic.RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429),
                    body={},
                )
            return MagicMock(content=[MagicMock(text=signals.model_dump_json())])

        async_anthropic_client.messages.create = flaky_create

        with (
            patch("backend.agents.scraper_agent._client", async_anthropic_client),
            patch("asyncio.sleep", AsyncMock()),
        ):
            result = await _call_with_retry(raw_venue, max_attempts=3)

        assert result is not None
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_after_all_retries_exhausted(
        self, raw_venue, async_anthropic_client
    ):
        import anthropic

        async def always_rate_limited(**_):
            raise anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429),
                body={},
            )

        async_anthropic_client.messages.create = always_rate_limited

        with (
            patch("backend.agents.scraper_agent._client", async_anthropic_client),
            patch("asyncio.sleep", AsyncMock()),
        ):
            result = await _call_with_retry(raw_venue, max_attempts=2)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_json_parse_error(
        self, raw_venue, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="not valid json {{{")]
        )

        with patch("backend.agents.scraper_agent._client", async_anthropic_client):
            result = await _call_with_retry(raw_venue)

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_prompt_caching_header(
        self, raw_venue, async_anthropic_client
    ):
        signals = ExtractedSignals()
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=signals.model_dump_json())]
        )

        with patch("backend.agents.scraper_agent._client", async_anthropic_client):
            await _call_with_retry(raw_venue)

        call_kwargs = async_anthropic_client.messages.create.call_args[1]
        system = call_kwargs["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}


# ─── ScraperAgent.run ────────────────────────────────────────────────────────

class TestScraperAgentRun:
    def _make_nimble_response(self, venues: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "organic_results": [
                {"title": v["name"], "link": v.get("url"), "snippet": v.get("snippet", ""), "displayed_link": ""}
                for v in venues
            ]
        }
        return resp

    @pytest.mark.asyncio
    async def test_run_returns_list_of_dicts(
        self, birthday_intent, async_anthropic_client
    ):
        nimble_resp = self._make_nimble_response([
            {"name": "Locanda Verde", "snippet": "Great Italian with private rooms."},
        ])
        signals = ExtractedSignals(special_occasion_score=80, noise_level=NoiseLevel.QUIET)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=nimble_resp)
        mock_http.aclose = AsyncMock()

        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=signals.model_dump_json())]
        )

        with (
            patch("backend.agents.scraper_agent._client", async_anthropic_client),
            patch("httpx.AsyncClient", return_value=mock_http),
        ):
            agent = ScraperAgent()
            result = await agent.run(birthday_intent)

        assert isinstance(result, list)
        assert all(isinstance(v, dict) for v in result)

    @pytest.mark.asyncio
    async def test_nimble_exceptions_are_swallowed(
        self, birthday_intent, async_anthropic_client
    ):
        """A failing Nimble query should not crash the whole run."""
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=Exception("Nimble down"))
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            agent = ScraperAgent()
            result = await agent.run(birthday_intent)

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_deduplication_applied_before_extraction(
        self, birthday_intent, async_anthropic_client
    ):
        """Duplicate venue names from different queries are collapsed before Claude."""
        dup_venue = {"name": "Locanda Verde", "snippet": "Italian NYC private room"}
        nimble_resp = self._make_nimble_response([dup_venue, dup_venue, dup_venue])

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=nimble_resp)
        mock_http.aclose = AsyncMock()

        signals = ExtractedSignals(special_occasion_score=80)
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=signals.model_dump_json())]
        )

        with (
            patch("backend.agents.scraper_agent._client", async_anthropic_client),
            patch("httpx.AsyncClient", return_value=mock_http),
        ):
            agent = ScraperAgent()
            await agent.run(birthday_intent)

        # Three queries × 3 results = 9, but all same name → 1 unique → 1 Claude call
        assert async_anthropic_client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_http_client_closed_after_run(
        self, birthday_intent, async_anthropic_client
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=Exception("err"))
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            agent = ScraperAgent()
            await agent.run(birthday_intent)

        mock_http.aclose.assert_called_once()
