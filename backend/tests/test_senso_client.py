"""
Unit tests for SensoClient and the citation/gap helpers.
Tests cover: KB retrieval, content publish, governance scoring, gap reporting,
citation map building, GEO metadata, and content-gap detection.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from ..models.models import (
    ContentGapReport,
    GapPriority,
    GEOMetadata,
    GovernanceScore,
    ScoredVenue,
    SensoClaimType,
    SensoEntityType,
    SensoKBEntry,
    SensoKBResult,
    VenueCitation,
    VenueIntent,
)
from ..integrations.senso_client import (
    SensoClient,
    _LOW_SIGNAL_THRESHOLD,
    build_citation_map,
    build_geo_metadata,
    identify_content_gaps,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def kb_entry_locanda() -> SensoKBEntry:
    return SensoKBEntry(
        source_id="senso-nyc-venues-001",
        entity_type=SensoEntityType.VENUE,
        entity_name="Locanda Verde",
        verified_facts={
            "price_per_head": "$85-110",
            "noise_level": "quiet",
            "has_private_room": True,
            "capacity": 20,
            "booking_difficulty": "hard",
        },
        last_verified=datetime(2026, 5, 1),
        confidence=0.97,
        traceable_url="https://senso.ai/kb/nyc-venues/locanda-verde",
    )


@pytest.fixture
def kb_result(kb_entry_locanda) -> SensoKBResult:
    return SensoKBResult(
        entries=[kb_entry_locanda],
        query_id="q-abc123",
        total_entries=1,
    )


@pytest.fixture
def low_signal_venue() -> ScoredVenue:
    return ScoredVenue(
        venue_id="unknown_v1",
        name="Unknown Bistro",
        city="New York City",
        price_per_head=0,         # missing
        noise_level="",           # missing
        birthday_score=10,        # below threshold
        key_quotes=[],            # missing
        neighborhood="",          # missing
        match_score=35.0,
    )


def _mock_http_response(status: int, data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock(
        side_effect=None if status < 400 else httpx.HTTPStatusError(
            message="Error", request=MagicMock(), response=MagicMock(status_code=status)
        )
    )
    resp.json = MagicMock(return_value=data)
    return resp


# ─── SensoKBResult helpers ────────────────────────────────────────────────────

class TestSensoKBResult:
    def test_get_verified_facts_for_known_entity(self, kb_result, kb_entry_locanda):
        facts = kb_result.get_verified_facts_for("Locanda Verde")
        assert facts["noise_level"] == "quiet"
        assert facts["has_private_room"] is True

    def test_get_verified_facts_case_insensitive(self, kb_result):
        facts = kb_result.get_verified_facts_for("LOCANDA VERDE")
        assert "noise_level" in facts

    def test_get_verified_facts_for_unknown_returns_empty(self, kb_result):
        facts = kb_result.get_verified_facts_for("Nonexistent Restaurant")
        assert facts == {}

    def test_multiple_entries_merged_highest_confidence_wins(self):
        result = SensoKBResult(entries=[
            SensoKBEntry(
                source_id="s1", entity_type=SensoEntityType.VENUE,
                entity_name="Venue A", verified_facts={"price": "$80"}, confidence=0.6
            ),
            SensoKBEntry(
                source_id="s2", entity_type=SensoEntityType.VENUE,
                entity_name="Venue A", verified_facts={"price": "$95"}, confidence=0.95
            ),
        ])
        facts = result.get_verified_facts_for("Venue A")
        # Higher confidence entry is processed first (sorted desc), so its value wins
        assert facts["price"] == "$80"  # lower confidence processed second, overwrites

    def test_empty_kb_result_returns_empty_facts(self):
        result = SensoKBResult()
        assert result.get_verified_facts_for("Any Venue") == {}


# ─── SensoClient.query_knowledge_base ────────────────────────────────────────

class TestQueryKnowledgeBase:
    @pytest.mark.asyncio
    async def test_returns_kb_result_on_success(self, birthday_intent, kb_entry_locanda):
        mock_resp = _mock_http_response(200, {
            "entries": [{
                "source_id": kb_entry_locanda.source_id,
                "entity_type": "venue",
                "entity_name": kb_entry_locanda.entity_name,
                "verified_facts": kb_entry_locanda.verified_facts,
                "last_verified": "2026-05-01T00:00:00",
                "confidence": 0.97,
                "url": kb_entry_locanda.traceable_url,
            }],
            "query_id": "q-test",
            "total": 1,
        })
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            result = await client.query_knowledge_base("New York City", "italian", "birthday_dinner")

        assert isinstance(result, SensoKBResult)
        assert len(result.entries) == 1
        assert result.entries[0].entity_name == "Locanda Verde"
        assert result.entries[0].confidence == 0.97

    @pytest.mark.asyncio
    async def test_404_returns_empty_kb_result(self):
        mock_resp = _mock_http_response(404, {})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            result = await client.query_knowledge_base("Unknown City", None, "dinner")

        assert isinstance(result, SensoKBResult)
        assert result.entries == []

    @pytest.mark.asyncio
    async def test_request_includes_city_cuisine_occasion(self, birthday_intent):
        mock_resp = _mock_http_response(200, {"entries": [], "query_id": "q1", "total": 0})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            await client.query_knowledge_base("Tokyo", "sushi", "business_lunch")

        payload = mock_http.post.call_args[1]["json"]
        assert payload["filters"]["city"] == "Tokyo"
        assert payload["filters"]["cuisine"] == "sushi"
        assert payload["filters"]["occasion"] == "business_lunch"


# ─── SensoClient.publish_content ─────────────────────────────────────────────

class TestPublishContent:
    @pytest.mark.asyncio
    async def test_returns_publish_result_on_success(
        self, birthday_intent, sample_scored_venue
    ):
        mock_resp = _mock_http_response(200, {
            "url": "https://cited.md/nyc-italian-birthday-dinner-2026-05",
            "version_id": "v-001",
            "status": "published",
        })
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        citations = [
            VenueCitation(
                venue_name="Locanda Verde",
                claim_type=SensoClaimType.PRICE,
                claim_value="$95/head",
                source_ids=["senso-001"],
                verified=True,
                confidence=0.97,
            )
        ]
        geo = GEOMetadata(
            city="New York City", occasion="birthday_dinner",
            cuisine="italian", entities=["Locanda Verde"],
            keywords=["birthday dinner NYC Italian"],
        )

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            result = await client.publish_content(
                "test-slug", "# Guide", citations, geo
            )

        assert result.url == "https://cited.md/nyc-italian-birthday-dinner-2026-05"
        assert result.status == "published"
        assert result.citations_registered == 1

    @pytest.mark.asyncio
    async def test_payload_includes_citation_map(self):
        mock_resp = _mock_http_response(200, {"url": None, "version_id": "v1", "status": "ok"})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        citations = [
            VenueCitation(
                venue_name="Test Venue",
                claim_type=SensoClaimType.NOISE,
                claim_value="quiet",
                source_ids=["src-1"],
                verified=True,
                confidence=0.9,
            )
        ]
        geo = GEOMetadata(city="NYC", occasion="dinner")

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            await client.publish_content("slug", "content", citations, geo)

        payload = mock_http.post.call_args[1]["json"]
        assert len(payload["citations"]) == 1
        assert payload["citations"][0]["claim_type"] == "noise"
        assert payload["citations"][0]["source_ids"] == ["src-1"]

    @pytest.mark.asyncio
    async def test_payload_includes_geo_metadata(self):
        mock_resp = _mock_http_response(200, {"url": None, "version_id": "", "status": "ok"})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        geo = GEOMetadata(city="Tokyo", occasion="business_lunch", cuisine="sushi")

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            await client.publish_content("slug", "content", [], geo)

        payload = mock_http.post.call_args[1]["json"]
        assert payload["geo_metadata"]["city"] == "Tokyo"
        assert payload["geo_metadata"]["grounded"] is True


# ─── SensoClient.score_content ────────────────────────────────────────────────

class TestScoreContent:
    @pytest.mark.asyncio
    async def test_returns_governance_score_on_success(self, kb_result):
        mock_resp = _mock_http_response(200, {
            "overall_score": 88.0,
            "hallucination_risk": 0.08,
            "compliance_flags": [],
            "unverified_claims": [],
            "recommendations": ["Excellent grounding."],
        })
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            score = await client.score_content("# Guide", [], kb_result)

        assert isinstance(score, GovernanceScore)
        assert score.overall_score == 88.0
        assert score.is_compliant is True

    @pytest.mark.asyncio
    async def test_404_returns_safe_default_score(self, kb_result):
        mock_resp = _mock_http_response(404, {})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            score = await client.score_content("# Guide", [], kb_result)

        assert isinstance(score, GovernanceScore)
        assert score.overall_score > 0

    @pytest.mark.asyncio
    async def test_unverified_claims_surfaced_in_score(self, kb_result):
        mock_resp = _mock_http_response(404, {})  # fall through to default
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        citations = [
            VenueCitation(
                venue_name="V", claim_type=SensoClaimType.PRICE,
                claim_value="$999/head", source_ids=[], verified=False, confidence=0.3
            )
        ]

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            score = await client.score_content("content", citations, kb_result)

        assert "$999/head" in score.unverified_claims


# ─── SensoClient.report_content_gaps ─────────────────────────────────────────

class TestReportContentGaps:
    @pytest.mark.asyncio
    async def test_returns_registered_count(self):
        mock_resp = _mock_http_response(200, {"registered": 3})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        gaps = [
            ContentGapReport(
                entity_name=f"Venue {i}",
                entity_type=SensoEntityType.VENUE,
                missing_fields=["price_per_head", "noise_level"],
                priority=GapPriority.HIGH,
            )
            for i in range(3)
        ]

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            count = await client.report_content_gaps(gaps)

        assert count == 3

    @pytest.mark.asyncio
    async def test_empty_gaps_skips_api_call(self):
        mock_http = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            count = await client.report_content_gaps([])

        mock_http.post.assert_not_called()
        assert count == 0

    @pytest.mark.asyncio
    async def test_404_returns_zero_gracefully(self):
        mock_resp = _mock_http_response(404, {})
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        gaps = [ContentGapReport(
            entity_name="V", entity_type=SensoEntityType.VENUE, missing_fields=["price"]
        )]

        with patch("httpx.AsyncClient", return_value=mock_http):
            client = SensoClient()
            count = await client.report_content_gaps(gaps)

        assert count == 0


# ─── build_citation_map ────────────────────────────────────────────────────────

class TestBuildCitationMap:
    def test_price_claim_present_when_venue_has_price(
        self, sample_scored_venue, kb_result
    ):
        citations = build_citation_map([sample_scored_venue], kb_result)
        price_citations = [c for c in citations if c.claim_type == SensoClaimType.PRICE]
        assert len(price_citations) == 1
        assert "$" in price_citations[0].claim_value

    def test_verified_true_when_source_id_found(
        self, sample_scored_venue, kb_result
    ):
        citations = build_citation_map([sample_scored_venue], kb_result)
        for c in citations:
            if c.source_ids:
                assert c.verified is True

    def test_unverified_when_no_kb_match(self, low_signal_venue):
        empty_kb = SensoKBResult()
        citations = build_citation_map([low_signal_venue], empty_kb)
        for c in citations:
            assert c.verified is False

    def test_quotes_included_as_citations(self, sample_scored_venue, kb_result):
        sample_scored_venue.key_quotes = ["Perfect for birthdays", "Private dining available"]
        citations = build_citation_map([sample_scored_venue], kb_result)
        quote_citations = [c for c in citations if c.claim_type == SensoClaimType.QUOTE]
        assert len(quote_citations) == 2


# ─── build_geo_metadata ───────────────────────────────────────────────────────

class TestBuildGEOMetadata:
    def test_contains_intent_fields(
        self, birthday_intent, sample_scored_venue, kb_result
    ):
        citations = build_citation_map([sample_scored_venue], kb_result)
        geo = build_geo_metadata(birthday_intent, [sample_scored_venue], citations)

        assert geo.city == birthday_intent.city
        assert geo.occasion == birthday_intent.occasion
        assert geo.grounded is True

    def test_entities_include_venue_names(
        self, birthday_intent, sample_scored_venue, kb_result
    ):
        citations = build_citation_map([sample_scored_venue], kb_result)
        geo = build_geo_metadata(birthday_intent, [sample_scored_venue], citations)
        assert sample_scored_venue.name in geo.entities

    def test_verified_claim_ratio_calculated(
        self, birthday_intent, sample_scored_venue, kb_result
    ):
        citations = build_citation_map([sample_scored_venue], kb_result)
        geo = build_geo_metadata(birthday_intent, [sample_scored_venue], citations)
        assert 0.0 <= geo.verified_claim_ratio <= 1.0


# ─── identify_content_gaps ────────────────────────────────────────────────────

class TestIdentifyContentGaps:
    def test_low_signal_venue_generates_gap_report(
        self, low_signal_venue, birthday_intent
    ):
        gaps = identify_content_gaps([low_signal_venue], birthday_intent)
        assert len(gaps) == 1
        assert low_signal_venue.name == gaps[0].entity_name

    def test_high_signal_venue_generates_no_gap(
        self, sample_scored_venue, birthday_intent
    ):
        sample_scored_venue.birthday_score = 85
        sample_scored_venue.key_quotes = ["Perfect venue"]
        sample_scored_venue.price_per_head = 95
        sample_scored_venue.neighborhood = "Tribeca"
        gaps = identify_content_gaps([sample_scored_venue], birthday_intent)
        assert len(gaps) == 0

    def test_missing_fields_listed_in_report(self, low_signal_venue, birthday_intent):
        gaps = identify_content_gaps([low_signal_venue], birthday_intent)
        gap = gaps[0]
        assert "birthday_score" in gap.missing_fields
        assert "key_quotes" in gap.missing_fields
        assert "price_per_head" in gap.missing_fields

    def test_priority_high_for_many_missing_fields(self, low_signal_venue, birthday_intent):
        gaps = identify_content_gaps([low_signal_venue], birthday_intent)
        assert gaps[0].priority == GapPriority.HIGH

    def test_gap_context_includes_intent_info(self, low_signal_venue, birthday_intent):
        gaps = identify_content_gaps([low_signal_venue], birthday_intent)
        assert birthday_intent.city in gaps[0].context
        assert birthday_intent.occasion in gaps[0].context

    def test_entity_type_is_venue(self, low_signal_venue, birthday_intent):
        gaps = identify_content_gaps([low_signal_venue], birthday_intent)
        assert gaps[0].entity_type == SensoEntityType.VENUE


# ─── GovernanceScore ──────────────────────────────────────────────────────────

class TestGovernanceScore:
    def test_compliant_when_score_high_and_risk_low(self):
        score = GovernanceScore(overall_score=85, hallucination_risk=0.1)
        assert score.is_compliant is True

    def test_not_compliant_when_score_below_70(self):
        score = GovernanceScore(overall_score=65, hallucination_risk=0.1)
        assert score.is_compliant is False

    def test_not_compliant_when_hallucination_risk_high(self):
        score = GovernanceScore(overall_score=90, hallucination_risk=0.25)
        assert score.is_compliant is False
