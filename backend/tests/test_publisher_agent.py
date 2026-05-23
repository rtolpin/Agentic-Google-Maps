"""
Unit tests for PublisherAgent — guide generation, slug building, and Senso publish.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..models.models import VenueIntent, ScoredVenue, VenueIntelligence, PublishedGuide
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


# ─── PublisherAgent._generate_guide ──────────────────────────────────────────

class TestGenerateGuide:
    @pytest.mark.asyncio
    async def test_returns_string(
        self, birthday_intent, sample_scored_venue, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Guide\n\nSample content.")]
        )

        with patch("files.publisher_agent._client", async_anthropic_client):
            agent = PublisherAgent()
            result = await agent._generate_guide(birthday_intent, [sample_scored_venue])

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_uses_prompt_caching_header(
        self, birthday_intent, sample_scored_venue, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Guide")]
        )

        with patch("files.publisher_agent._client", async_anthropic_client):
            agent = PublisherAgent()
            await agent._generate_guide(birthday_intent, [sample_scored_venue])

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

        with patch("files.publisher_agent._client", async_anthropic_client):
            agent = PublisherAgent()
            await agent._generate_guide(birthday_intent, [sample_scored_venue])

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

        with patch("files.publisher_agent._client", async_anthropic_client):
            agent = PublisherAgent()
            await agent._generate_guide(birthday_intent, [sample_scored_venue])

        assert sample_intelligence.why_card[:30] in captured_prompt


# ─── PublisherAgent._publish_to_senso ────────────────────────────────────────

class TestPublishToSenso:
    @pytest.mark.asyncio
    async def test_posts_to_correct_url(self, birthday_intent):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"status": "published", "url": "https://cited.md/slug"}

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            agent = PublisherAgent()
            result = await agent._publish_to_senso("test-slug", "# Guide", birthday_intent)

        assert result["status"] == "published"
        call_args = mock_http.post.call_args
        assert "senso.ai" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_payload_contains_required_fields(self, birthday_intent):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            agent = PublisherAgent()
            await agent._publish_to_senso("test-slug", "# Guide content", birthday_intent)

        payload = mock_http.post.call_args[1]["json"]
        assert payload["slug"] == "test-slug"
        assert payload["content"] == "# Guide content"
        assert payload["metadata"]["city"] == birthday_intent.city
        assert payload["metadata"]["grounded"] is True


# ─── PublisherAgent.publish_guide (integration) ───────────────────────────────

class TestPublishGuide:
    @pytest.mark.asyncio
    async def test_returns_published_guide(
        self, birthday_intent, sample_scored_venue, async_anthropic_client
    ):
        async_anthropic_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Great Guide\n\nContent.")]
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"status": "published", "url": "https://cited.md/x"}

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with (
            patch("files.publisher_agent._client", async_anthropic_client),
            patch("httpx.AsyncClient", return_value=mock_http),
        ):
            agent = PublisherAgent()
            result = await agent.publish_guide(birthday_intent, [sample_scored_venue])

        assert isinstance(result, PublishedGuide)
        assert result.status == "published"
        assert "new-york-city" in result.slug
