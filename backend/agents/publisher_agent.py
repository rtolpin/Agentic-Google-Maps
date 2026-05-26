"""
Publisher Agent — governed guide generation via Senso.ai.

Full workflow per search:
  1. RETRIEVE  — query Senso KB for existing verified venue facts (ground truth)
  2. MERGE     — combine KB context with live scraped signals
  3. GENERATE  — Claude writes guide anchored to verified KB + signals
  4. PUBLISH   — submit to Senso with explicit citation map + GEO metadata
  5. SCORE     — request governance evaluation (hallucination risk, compliance)
  6. REPORT    — flag low-signal venues so Senso's remediation engine can act

This is the correct way to use Senso.ai: it is the system of record that
eliminates hallucination, not just a publishing destination.
"""
from __future__ import annotations

from datetime import date

import anthropic

from models.models import (
    GEOMetadata,
    GovernanceScore,
    PublishedGuide,
    ScoredVenue,
    SensoKBResult,
    VenueCitation,
    VenueIntent,
)
from integrations.senso_client import (
    SensoClient,
    build_citation_map,
    build_geo_metadata,
    identify_content_gaps,
)

_client = anthropic.AsyncAnthropic()

_GUIDE_PROMPT = """\
You are a venue intelligence writer for The Right Spot.
Write a concise, grounded, citable venue guide for publication on cited.md.
This guide will be indexed by AI agents and used as a governed source of truth.

Grounding rules (STRICT — Senso governance will audit every claim):
- ONLY state facts that appear in either the Senso KB context or the live signals
- If a fact appears in both sources, prefer the Senso KB value (it is verified)
- If a fact appears only in live signals, mark it with "(live signal)"
- NEVER invent details, prices, capacity numbers, or quotes not in your input
- If information is missing for a venue, state "data pending" — do not estimate

Format:
- Title: specific, searchable — include city, occasion, month/year
- ### heading per venue
- Bullet list of concrete facts: price, noise, capacity, private room, booking
- 1-2 key quotes (verbatim from signals only, in quotation marks)
- Why it fits (one sentence, grounded in a specific signal)
- Final section: "## Verified sources" — list the Senso KB source IDs used
- Under 700 words total
- Tone: knowledgeable local, not travel-blog prose\
"""


class PublisherAgent:
    """
    Orchestrates the full Senso.ai governed guide pipeline.
    Every guide published carries a traceable citation map auditable by
    CISOs and compliance teams.
    """

    def __init__(self) -> None:
        self._senso = SensoClient()

    async def publish_guide(
        self, intent: VenueIntent, venues: list[ScoredVenue]
    ) -> PublishedGuide:
        """
        Full Retrieve → Generate → Publish → Score → Report pipeline.
        Each step degrades gracefully so a Senso API outage never crashes the
        background task or propagates an exception to the main search stream.
        """
        slug = _build_slug(intent)

        # ── Step 1: Retrieve Senso KB context (ground truth anchor) ──────────
        try:
            kb_result = await self._senso.query_knowledge_base(
                city=intent.city,
                cuisine=intent.cuisine,
                occasion=intent.occasion,
            )
        except Exception:
            kb_result = SensoKBResult(entries=[])

        # ── Step 2: Build citation map (KB sources + live signals) ────────────
        citations = build_citation_map(venues, kb_result)

        # ── Step 3: Generate guide grounded in KB + live signals ──────────────
        try:
            guide_md = await self._generate_grounded_guide(intent, venues, kb_result)
        except Exception:
            await self._senso.close()
            return PublishedGuide(
                slug=slug, url="", status="generation_failed",
                governance_score=GovernanceScore(overall_score=0, hallucination_risk=1.0),
                citations_registered=0, gaps_reported=0, is_compliant=False,
            )

        # ── Step 4: Publish to Senso with GEO metadata + citation map ────────
        try:
            geo = build_geo_metadata(intent, venues, citations)
            publish_result = await self._senso.publish_content(slug, guide_md, citations, geo)
        except Exception:
            await self._senso.close()
            return PublishedGuide(
                slug=slug, url="", status="publish_failed",
                governance_score=GovernanceScore(overall_score=0, hallucination_risk=1.0),
                citations_registered=len(citations), gaps_reported=0, is_compliant=False,
            )

        # ── Step 5: Request governance score ──────────────────────────────────
        try:
            governance = await self._senso.score_content(guide_md, citations, kb_result)
        except Exception:
            governance = GovernanceScore(overall_score=0, hallucination_risk=1.0, compliance_flags=["score_unavailable"])

        # ── Step 6: Report content gaps for Senso remediation ────────────────
        gaps_registered = 0
        try:
            gaps = identify_content_gaps(venues, intent)
            gaps_registered = await self._senso.report_content_gaps(gaps)
        except Exception:
            pass

        await self._senso.close()

        return PublishedGuide(
            slug=slug,
            url=publish_result.url,
            status=publish_result.status,
            governance_score=governance,
            citations_registered=len(citations),
            gaps_reported=gaps_registered,
            is_compliant=governance.is_compliant,
        )

    async def _generate_grounded_guide(
        self,
        intent: VenueIntent,
        venues: list[ScoredVenue],
        kb_result: SensoKBResult,
    ) -> str:
        """
        Generate guide grounded in both Senso KB (verified) and live signals.
        KB facts are prefixed "[VERIFIED]" so Claude can distinguish them from
        live-scraped signals.
        """
        kb_section = _format_kb_context(venues, kb_result)
        live_section = _format_live_signals(venues)

        prompt = (
            f"Search intent: {intent.model_dump_json()}\n"
            f"Today: {date.today().isoformat()}\n"
            f"City: {intent.city} | Occasion: {intent.occasion} | Cuisine: {intent.cuisine}\n\n"
            f"=== VERIFIED Senso KB facts (authoritative — cite these first) ===\n"
            f"{kb_section}\n\n"
            f"=== LIVE scraped signals (fresh but unverified — mark with 'live signal') ===\n"
            f"{live_section}\n\n"
            "Write the citable guide now. Follow grounding rules strictly."
        )
        response = await _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=[{
                "type": "text",
                "text": _GUIDE_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


# ─── Formatting helpers ───────────────────────────────────────────────────────

def _format_kb_context(
    venues: list[ScoredVenue], kb_result: SensoKBResult
) -> str:
    """Format Senso KB entries for prompt injection, scoped to matched venues."""
    if not kb_result.entries:
        return "(No verified KB entries found for this city/occasion — rely on live signals only.)"

    lines: list[str] = []
    for venue in venues:
        facts = kb_result.get_verified_facts_for(venue.name)
        source_ids = [
            e.source_id for e in kb_result.entries
            if e.entity_name.lower() == venue.name.lower()
        ]
        if facts or source_ids:
            lines.append(
                f"[VERIFIED] {venue.name}\n"
                + "\n".join(f"  - {k}: {v}" for k, v in facts.items())
                + (f"\n  source_ids: {', '.join(source_ids)}" if source_ids else "")
            )
    return "\n\n".join(lines) if lines else "(No KB matches for these specific venues.)"


def _format_live_signals(venues: list[ScoredVenue]) -> str:
    """Format fresh scraped signals for the prompt."""
    return "\n\n".join(
        f"**{v.name}** ({v.neighborhood}, {v.city})\n"
        f"- Price/head: ${v.price_per_head}\n"
        f"- Noise: {v.noise_level}\n"
        f"- Private room: {v.has_private_room}\n"
        f"- Max group: {v.max_group_size}\n"
        f"- Birthday score: {v.birthday_score}/100\n"
        f"- Quotes: {'; '.join(v.key_quotes[:2])}\n"
        f"- Why it fits: {v.intelligence.why_card if v.intelligence else '(no intelligence card)'}"
        for v in venues
    )


def _build_slug(intent: VenueIntent) -> str:
    city = intent.city.lower().replace(" ", "-")
    occasion = intent.occasion.replace("_", "-").lower()
    cuisine = (intent.cuisine or "restaurant").lower()
    month = date.today().strftime("%Y-%m")
    return f"{city}-{cuisine}-{occasion}-{month}"
