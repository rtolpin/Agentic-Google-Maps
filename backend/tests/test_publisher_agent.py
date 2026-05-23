"""
Unit tests for PublisherAgent — guide generation, slug building, and Senso publish.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..models.models import (
    GovernanceScore,
    PublishedGuide,
    ScoredVenue,
    SensoKBResult,
    VenueIntelligence,
    VenueIntent,
)
from ..agents.publisher_agent import PublisherAgent, _build_slug


# ─── _build_slug ──────────────────────────────────────────────────────────────

class TestBuildSlug:
    def test_basic_slug(self):
        intent = VenueIntent(city="New York City", occasion="birthday_dinner", cuisine="italian")
        slug = _build_slug(intent)
        month = date.today().strftime("%Y-%m")
        assert slug == f"new-york-city-italian-birthday-dinner-{month}"

    def test_spaces_in_city_become_dashes(self):
        intent = VenueIntent(city="San Francisco", occasion="date_night")
        slug = _build_slug(intent)
        assert "san-francisco" in slug

    def test_underscores_in_occasion_become_dashes(self):
        intent = VenueIntent(city="Tokyo", occasion="business_lunch")
        slug = _build_slug(intent)
        assert "business-lunch" in slug

    def test_null_cuisine_defaults_to_restaurant(self):
        intent = VenueIntent(city="Paris", occasion="dinner")
        slug = _build_slug(intent)
        assert "restaurant" in slug

    def test_slug_is_lowercase(self):
        intent = VenueIntent(city="Rome", occasion="Birthday_Dinner", cuisine="ITALIAN")
        slug = _build_slug(intent)
        assert slug == slug.lower()


# ─── PublisherAgent._generate_grounded_guide ─────────────────────────────────

class TestGenerateGuide:
    @pytest.mark.asyncio
    async def test_returns_string(
        self, birthday_intent, sample_scored_venue, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Guide\n\nSample content.")]
        )

        with (
            patch("backend.agents.publisher_agent._client", async_anthropic_client),
            patch("backend.agents.publisher_agent.SensoClient"),
        ):
            agent = PublisherAgent()
            result = await agent._generate_grounded_guide(
                birthday_intent, [sample_scored_venue], SensoKBResult()
            )

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_uses_prompt_caching_header(
        self, birthday_intent, sample_scored_venue, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Guide")]
        )

        with (
            patch("backend.agents.publisher_agent._client", async_anthropic_client),
            patch("backend.agents.publisher_agent.SensoClient"),
        ):
            agent = PublisherAgent()
            await agent._generate_grounded_guide(
                birthday_intent, [sample_scored_venue], SensoKBResult()
            )

        call_kwargs = async_anthropic_client.messages.create.call_args[1]
        assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_guide_prompt_includes_venue_name(
        self, birthday_intent, sample_scored_venue, async_anthropic_client
    ):
        captured_prompt = None

        async def capture(**kwargs):
            nonlocal captured_prompt
            captured_prompt = kwargs["messages"][0]["content"]
            return MagicMock(content=[MagicMock(text="# Guide")])

        async_anthropic_client.messages.create = capture

        with (
            patch("backend.agents.publisher_agent._client", async_anthropic_client),
            patch("backend.agents.publisher_agent.SensoClient"),
        ):
            agent = PublisherAgent()
            await agent._generate_grounded_guide(
                birthday_intent, [sample_scored_venue], SensoKBResult()
            )

        assert sample_scored_venue.name in captured_prompt

    @pytest.mark.asyncio
    async def test_intelligence_why_card_included_when_present(
        self, birthday_intent, sample_scored_venue, sample_intelligence, async_anthropic_client
    ):
        sample_scored_venue.intelligence = sample_intelligence
        captured_prompt = None

        async def capture(**kwargs):
            nonlocal captured_prompt
            captured_prompt = kwargs["messages"][0]["content"]
            return MagicMock(content=[MagicMock(text="# Guide")])

        async_anthropic_client.messages.create = capture

        with (
            patch("backend.agents.publisher_agent._client", async_anthropic_client),
            patch("backend.agents.publisher_agent.SensoClient"),
        ):
            agent = PublisherAgent()
            await agent._generate_grounded_guide(
                birthday_intent, [sample_scored_venue], SensoKBResult()
            )

        assert sample_intelligence.why_card[:30] in captured_prompt


# ─── PublisherAgent.publish_guide — Senso integration ────────────────────────

class TestPublishToSenso:
    def _mock_senso(self) -> AsyncMock:
        senso = AsyncMock()
        senso.query_knowledge_base = AsyncMock(return_value=SensoKBResult())
        senso.publish_content = AsyncMock(
            return_value=MagicMock(url="https://cited.md/slug", status="published")
        )
        senso.score_content = AsyncMock(
            return_value=GovernanceScore(overall_score=85, hallucination_risk=0.1)
        )
        senso.report_content_gaps = AsyncMock(return_value=0)
        senso.close = AsyncMock()
        return senso

    @pytest.mark.asyncio
    async def test_publish_content_called_with_correct_slug(
        self, birthday_intent, async_anthropic_client
    ):
        mock_senso = self._mock_senso()
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Guide")]
        )

        with (
            patch("backend.agents.publisher_agent._client", async_anthropic_client),
            patch("backend.agents.publisher_agent.SensoClient", return_value=mock_senso),
        ):
            agent = PublisherAgent()
            await agent.publish_guide(birthday_intent, [])

        slug = mock_senso.publish_content.call_args[0][0]
        assert "new-york-city" in slug
        assert "birthday" in slug

    @pytest.mark.asyncio
    async def test_geo_metadata_grounded_flag_is_true(
        self, birthday_intent, async_anthropic_client
    ):
        mock_senso = self._mock_senso()
        captured_geo = None

        async def capture_publish(slug, content, citations, geo):
            nonlocal captured_geo
            captured_geo = geo
            return MagicMock(url="https://cited.md/x", status="published")

        mock_senso.publish_content = capture_publish
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Guide")]
        )

        with (
            patch("backend.agents.publisher_agent._client", async_anthropic_client),
            patch("backend.agents.publisher_agent.SensoClient", return_value=mock_senso),
        ):
            agent = PublisherAgent()
            await agent.publish_guide(birthday_intent, [])

        assert captured_geo.city == birthday_intent.city
        assert captured_geo.grounded is True


# ─── PublisherAgent.publish_guide (integration) ───────────────────────────────

class TestPublishGuide:
    @pytest.mark.asyncio
    async def test_returns_published_guide(
        self, birthday_intent, sample_scored_venue, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Great Guide\n\nContent.")]
        )
        mock_senso = AsyncMock()
        mock_senso.query_knowledge_base = AsyncMock(return_value=SensoKBResult())
        mock_senso.publish_content = AsyncMock(
            return_value=MagicMock(url="https://cited.md/x", status="published")
        )
        mock_senso.score_content = AsyncMock(
            return_value=GovernanceScore(overall_score=85, hallucination_risk=0.1)
        )
        mock_senso.report_content_gaps = AsyncMock(return_value=0)
        mock_senso.close = AsyncMock()

        with (
            patch("backend.agents.publisher_agent._client", async_anthropic_client),
            patch("backend.agents.publisher_agent.SensoClient", return_value=mock_senso),
        ):
            agent = PublisherAgent()
            result = await agent.publish_guide(birthday_intent, [sample_scored_venue])

        assert isinstance(result, PublishedGuide)
        assert result.status == "published"
        assert "new-york-city" in result.slug
