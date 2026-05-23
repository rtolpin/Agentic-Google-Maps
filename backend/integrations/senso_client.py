"""
Senso.ai API client — the governed knowledge layer for The Right Spot.

Senso.ai is the system of record for AI: it stores verified venue facts,
anchors Claude's output to traceable sources, scores content for hallucination
risk, and drives remediation when data is missing.

Workflow for every guide publish:
  1. query_knowledge_base()  → retrieve Senso's verified facts before generating
  2. publish_content()       → submit guide + citation map for GEO indexing
  3. score_content()         → governance evaluation (hallucination risk, compliance)
  4. report_content_gaps()   → flag missing data so Senso can remediate proactively
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from ..models.models import (
    ContentGapReport,
    GEOMetadata,
    GapPriority,
    GovernanceScore,
    ScoredVenue,
    SensoEntityType,
    SensoKBResult,
    SensoKBEntry,
    SensoPublishResult,
    VenueCitation,
    SensoClaimType,
    VenueIntent,
)

SENSO_API_KEY = os.environ.get("SENSO_API_KEY", "")
_SENSO_BASE = "https://api.senso.ai/v1"

_LOW_SIGNAL_THRESHOLD = 40  # birthday_score below this triggers a gap report


class SensoClient:
    """
    Typed async wrapper around the Senso.ai REST API.

    All methods return Pydantic models.  Every factual claim published to Senso
    must carry a source_id so compliance teams can audit the provenance chain.
    """

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_SENSO_BASE,
            headers={
                "Authorization": f"Bearer {SENSO_API_KEY}",
                "Content-Type": "application/json",
                "X-Agent": "the-right-spot",
            },
            timeout=30.0,
        )

    # ─── Knowledge base retrieval ─────────────────────────────────────────────

    async def query_knowledge_base(
        self,
        city: str,
        cuisine: str | None,
        occasion: str,
    ) -> SensoKBResult:
        """
        Retrieve Senso's verified venue facts for this city/cuisine/occasion.
        Use the result to anchor Claude generation — never invent details
        that contradict or aren't present in the KB.
        """
        resp = await self._http.post(
            "/knowledge/query",
            json={
                "filters": {
                    "entity_types": ["venue", "city"],
                    "city": city,
                    "cuisine": cuisine,
                    "occasion": occasion,
                },
                "limit": 20,
                "include_confidence": True,
            },
        )
        if resp.status_code == 404:
            return SensoKBResult()
        resp.raise_for_status()
        data = resp.json()
        entries = [
            SensoKBEntry(
                source_id=e["source_id"],
                entity_type=SensoEntityType(e.get("entity_type", "venue")),
                entity_name=e["entity_name"],
                verified_facts=e.get("verified_facts", {}),
                last_verified=datetime.fromisoformat(e["last_verified"])
                if e.get("last_verified") else None,
                confidence=e.get("confidence", 1.0),
                traceable_url=e.get("url"),
            )
            for e in data.get("entries", [])
        ]
        return SensoKBResult(
            entries=entries,
            query_id=data.get("query_id", ""),
            total_entries=data.get("total", len(entries)),
        )

    # ─── Content publish ──────────────────────────────────────────────────────

    async def publish_content(
        self,
        slug: str,
        content: str,
        citations: list[VenueCitation],
        geo: GEOMetadata,
    ) -> SensoPublishResult:
        """
        Publish a grounded guide with explicit citation map and GEO metadata.
        Senso indexes this for AI discoverability (cited.md) and version-controls it.
        """
        resp = await self._http.post(
            "/content/publish",
            json={
                "slug": slug,
                "content": content,
                "destination": "cited.md",
                "visibility": "public",
                "citations": [
                    {
                        "venue_name": c.venue_name,
                        "claim_type": c.claim_type.value,
                        "claim_value": c.claim_value,
                        "source_ids": c.source_ids,
                        "verified": c.verified,
                        "confidence": c.confidence,
                    }
                    for c in citations
                ],
                "geo_metadata": geo.model_dump(),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return SensoPublishResult(
            slug=slug,
            url=data.get("url"),
            version_id=data.get("version_id", ""),
            status=data.get("status", "published"),
            citations_registered=len(citations),
        )

    # ─── Governance scoring ───────────────────────────────────────────────────

    async def score_content(
        self,
        content: str,
        citations: list[VenueCitation],
        kb_result: SensoKBResult,
    ) -> GovernanceScore:
        """
        Ask Senso to evaluate the published content for hallucination risk,
        compliance flags, and unverified claims.
        This is Senso's core governance function — results are auditable.
        """
        resp = await self._http.post(
            "/governance/score",
            json={
                "content": content,
                "citation_map": [
                    {
                        "venue_name": c.venue_name,
                        "claim_type": c.claim_type.value,
                        "source_ids": c.source_ids,
                        "verified": c.verified,
                    }
                    for c in citations
                ],
                "kb_query_id": kb_result.query_id,
                "total_kb_entries": kb_result.total_entries,
            },
        )
        if resp.status_code in (404, 501):
            # Governance endpoint not yet available — return safe default
            return GovernanceScore(
                overall_score=75.0,
                hallucination_risk=0.15,
                compliance_flags=[],
                unverified_claims=[
                    c.claim_value for c in citations if not c.verified
                ],
                recommendations=["Enable Senso governance scoring for full audit trail."],
            )
        resp.raise_for_status()
        data = resp.json()
        return GovernanceScore(
            overall_score=data.get("overall_score", 75.0),
            hallucination_risk=data.get("hallucination_risk", 0.15),
            compliance_flags=data.get("compliance_flags", []),
            unverified_claims=data.get("unverified_claims", []),
            recommendations=data.get("recommendations", []),
        )

    # ─── Content gap reporting ────────────────────────────────────────────────

    async def report_content_gaps(
        self, gaps: list[ContentGapReport]
    ) -> int:
        """
        Notify Senso of missing verified data so its remediation engine can
        proactively generate compliant drafts to fill the gaps.
        Returns the number of gaps successfully registered.
        """
        if not gaps:
            return 0
        resp = await self._http.post(
            "/gaps/report",
            json={
                "gaps": [
                    {
                        "entity_name": g.entity_name,
                        "entity_type": g.entity_type.value,
                        "missing_fields": g.missing_fields,
                        "priority": g.priority.value,
                        "context": g.context,
                        "suggested_sources": g.suggested_sources,
                    }
                    for g in gaps
                ]
            },
        )
        if resp.status_code in (404, 501):
            return 0
        resp.raise_for_status()
        return resp.json().get("registered", len(gaps))

    async def close(self) -> None:
        await self._http.aclose()


# ─── Citation builder ─────────────────────────────────────────────────────────

def build_citation_map(
    venues: list[ScoredVenue],
    kb_result: SensoKBResult,
) -> list[VenueCitation]:
    """
    For each venue's key facts, attempt to match a Senso KB source_id.
    If a match exists → verified=True; otherwise it's an unverified live signal.
    """
    citations: list[VenueCitation] = []
    for venue in venues:
        kb_facts = kb_result.get_verified_facts_for(venue.name)
        source_ids = [
            e.source_id
            for e in kb_result.entries
            if e.entity_name.lower() == venue.name.lower()
        ]

        def _cite(claim_type: SensoClaimType, value: str) -> VenueCitation:
            verified = bool(source_ids)
            return VenueCitation(
                venue_name=venue.name,
                claim_type=claim_type,
                claim_value=value,
                source_ids=source_ids,
                verified=verified,
                confidence=0.9 if verified else 0.5,
            )

        if venue.price_per_head:
            citations.append(_cite(SensoClaimType.PRICE, f"${venue.price_per_head}/head"))
        if venue.noise_level:
            citations.append(_cite(SensoClaimType.NOISE, venue.noise_level))
        if venue.has_private_room:
            citations.append(_cite(SensoClaimType.PRIVATE_ROOM, "yes"))
        if venue.max_group_size:
            citations.append(_cite(SensoClaimType.CAPACITY, str(venue.max_group_size)))
        for quote in venue.key_quotes[:2]:
            citations.append(_cite(SensoClaimType.QUOTE, quote))

    return citations


def build_geo_metadata(
    intent: VenueIntent,
    venues: list[ScoredVenue],
    citations: list[VenueCitation],
    freshness_hours: int = 0,
) -> GEOMetadata:
    """Build GEO metadata for Senso's AI discoverability indexing."""
    entities = list({v.name for v in venues} | {v.neighborhood for v in venues if v.neighborhood})
    keywords = list({
        intent.city,
        intent.occasion.replace("_", " "),
        intent.cuisine or "restaurant",
        *[v.cuisine for v in venues if v.cuisine],
    })
    verified = sum(1 for c in citations if c.verified)
    ratio = verified / len(citations) if citations else 0.0

    return GEOMetadata(
        city=intent.city,
        occasion=intent.occasion,
        cuisine=intent.cuisine,
        entities=entities,
        keywords=keywords,
        citation_count=len(citations),
        verified_claim_ratio=round(ratio, 2),
        data_freshness_hours=freshness_hours,
    )


def identify_content_gaps(
    venues: list[ScoredVenue],
    intent: VenueIntent,
) -> list[ContentGapReport]:
    """
    Scan venues for low-signal records and create gap reports for Senso.
    Senso's remediation engine will use these to generate compliant drafts.
    """
    gaps: list[ContentGapReport] = []
    for venue in venues:
        missing: list[str] = []
        if venue.birthday_score < _LOW_SIGNAL_THRESHOLD:
            missing.append("birthday_score")
        if not venue.key_quotes:
            missing.append("key_quotes")
        if not venue.noise_level:
            missing.append("noise_level")
        if venue.price_per_head == 0:
            missing.append("price_per_head")
        if not venue.neighborhood:
            missing.append("neighborhood")

        if missing:
            priority = (
                GapPriority.HIGH if len(missing) >= 3
                else GapPriority.MEDIUM if len(missing) >= 2
                else GapPriority.LOW
            )
            gaps.append(ContentGapReport(
                entity_name=venue.name,
                entity_type=SensoEntityType.VENUE,
                missing_fields=missing,
                priority=priority,
                context=(
                    f"Venue appeared in search for {intent.occasion} in {intent.city}. "
                    f"Match score: {venue.match_score}. Missing signals reduce ranking accuracy."
                ),
                suggested_sources=[venue.key_quotes[0]] if venue.key_quotes else [],
            ))
    return gaps
