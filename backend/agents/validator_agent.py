"""
Validator Agent — lightweight intent confidence checker.

Runs in parallel with ScraperAgent and GlobalIntelligenceAgent.
Returns a validation summary dict yielded as {"event": "validation", "data": ...}.
"""
from __future__ import annotations

from ..models.models import VenueIntent


class ValidatorAgent:
    """
    Checks the parsed intent for completeness and flags low-confidence signals.
    No external I/O — fast synchronous logic wrapped in an async interface.
    """

    async def run(self, intent: VenueIntent) -> dict:
        flags: list[str] = []
        confidence = 1.0

        if not intent.cuisine:
            flags.append("cuisine_unspecified")
            confidence -= 0.1

        if intent.group_size > 20 and not intent.needs_private_room:
            flags.append("large_group_no_private_room_requested")
            confidence -= 0.1

        if intent.group_size == 1:
            flags.append("solo_diner")

        if intent.occasion in ("birthday_dinner", "birthday_party") and intent.group_size < 2:
            flags.append("birthday_occasion_solo")
            confidence -= 0.05

        if intent.price_band is None:
            flags.append("price_band_unspecified")
            confidence -= 0.05

        if intent.noise_preference is None:
            flags.append("noise_preference_unspecified")

        for sig in intent.other_signals:
            if any(kw in sig.lower() for kw in ("vegan", "vegetarian", "gluten", "halal", "kosher")):
                if sig.lower() not in [r.lower() for r in intent.dietary_restrictions]:
                    flags.append(f"dietary_signal_in_other: {sig}")

        return {
            "confidence": round(max(0.0, min(1.0, confidence)), 2),
            "flags": flags,
            "intent_complete": len(flags) == 0,
            "occasion": intent.occasion,
            "city": intent.city,
            "group_size": intent.group_size,
        }
