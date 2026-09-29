"""
LLM-as-judge for why-card synthesis quality.

Deterministic guardrails catch *known* failure shapes (tone words, $ amounts,
city leakage). The judge covers what regexes can't: is the card faithful to
the venue data as a whole, is the tone right, does it actually help the user?

The judge is itself validated by the `judge_calibration` suite, which checks
it agrees with the hand-labelled outputs in synthesis_cases.json before its
scores are trusted.
"""
from __future__ import annotations

import json
import os
from typing import Any

import anthropic
from pydantic import BaseModel

JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-5-5")

_JUDGE_SYSTEM = """\
You are a strict evaluator for The Right Spot, a venue-recommendation app.
You grade one "intelligence card" (why_card, scenario, suggestions) that was
written about a venue for a user's search intent.

Grade ONLY against the VENUE DATA provided. Anything stated as fact that is not
supported by the venue data (dishes, prices, amenities, locations, awards)
counts as unfaithful, even if plausible. Hedged statements are fine.

Scores are integers 1-5:
- faithfulness: 5 = every factual claim is supported; 1 = major invented facts
- tone_fit: 5 = language matches the venue's real character (noise, price,
  cuisine); 1 = e.g. "romantic" or "upscale" for a loud cheap counter spot
- helpfulness: 5 = clearly tells this user whether/why the venue fits their
  intent, including honest caveats; 1 = generic or misleading

For each scenario-specific criterion, decide pass/fail.
Set overall_pass true only if faithfulness >= 4, tone_fit >= 4, and every
criterion passes.\
"""


class CriterionResult(BaseModel):
    criterion: str
    passed: bool
    reason: str


class JudgeVerdict(BaseModel):
    faithfulness: int
    tone_fit: int
    helpfulness: int
    criteria: list[CriterionResult]
    overall_pass: bool
    rationale: str


class Judge:
    def __init__(self, model: str = JUDGE_MODEL) -> None:
        self.model = model
        self._client = anthropic.AsyncAnthropic(timeout=120.0, max_retries=3)

    async def grade(
        self,
        intent: dict[str, Any],
        venue: dict[str, Any],
        output: dict[str, Any],
        criteria: list[str],
    ) -> JudgeVerdict | None:
        """Return a verdict, or None if the judge refused / failed to produce one."""
        prompt = (
            f"<search_intent>\n{json.dumps(intent, indent=2)}\n</search_intent>\n\n"
            f"<venue_data>\n{json.dumps(venue, indent=2)}\n</venue_data>\n\n"
            f"<card_to_grade>\n{json.dumps(output, indent=2)}\n</card_to_grade>\n\n"
            "<criteria>\n" + "\n".join(f"- {c}" for c in criteria) + "\n</criteria>"
        )
        response = await self._client.beta.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=JudgeVerdict,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        verdict = response.parsed_output
        if verdict is None:
            return None
        for name in ("faithfulness", "tone_fit", "helpfulness"):
            setattr(verdict, name, max(1, min(5, getattr(verdict, name))))
        return verdict
