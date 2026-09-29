"""
Output guardrails — verify LLM output before it reaches a user or gets published.

The synthesis and guide prompts already *ask* Claude to stay grounded and to
match tone to the venue. These checks *enforce* those rules in code, so a
prompt regression or an indirect injection from scraped review text can't
ship a wrong claim to the UI or to a public Senso guide.

Philosophy: a violation is raised only when venue data *contradicts* the
output (e.g. "romantic" for a venue we know is loud). Unknown data (price 0,
empty noise level) never triggers a violation.

  sanitize_intent()        — bound every field of a parsed VenueIntent
  check_intelligence()     — why-card / scenario / suggestions for one venue
  check_guide()            — full markdown guide before Senso publish
  filter_grounded_quotes() — drop extracted key_quotes not found in the source text
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

Severity = Literal["block", "warn"]


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    severity: Severity = "block"


def blocking(violations: Iterable[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == "block"]


class OutputGuardrailError(Exception):
    """Raised when LLM output fails a blocking guardrail. Callers fall back."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        super().__init__("; ".join(f"{v.code}: {v.detail}" for v in violations))


# ─── Shared helpers ───────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9']+")
_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$\s?(\d{1,4})(?:\.\d{2})?")
# A tone word is "negated" when a negation cue appears earlier in the same clause
# ("doesn't suit a quiet, romantic dinner", "falls short of the intimate…",
# "a mismatch with your request for a quiet…") or when it describes what the user
# wants ("…the refined experience you're after").
_NEGATION_CUE_RE = re.compile(
    r"\b(?:not|never|no|nothing|none|isn't|isnt|aren't|wasn't|doesn't|doesnt|don't|won't|"
    r"hardly|lacks?|lacking|without|anything\s+but|far\s+from|falls?\s+short\s+of|"
    r"rather\s+than|instead\s+of|than|mismatch|request\s+for|you\s+asked\s+for|"
    r"looking\s+for|searched\s+for|search\s+for)\b"
)
# The searched city may be named only to place the venue outside it
# ("in Trenton, NJ — outside New York City, where you searched").
_OUTSIDE_CUE_RE = re.compile(r"\b(?:outside|beyond|away\s+from|not\s+in|from|than)\b")
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?\u2014]|\b(?:but|however|yet|while|although)\b")
_DESIRE_AFTER_RE = re.compile(
    r"^[^.;:!?\u2014]{0,40}?\b(?:you'?re\s+(?:after|looking\s+for|hoping\s+for)|"
    r"you\s+(?:want|asked\s+for|need)|you\s+have\s+in\s+mind)\b"
)

_LEAKAGE_MARKERS = (
    "system prompt", "as an ai", "language model", "<user_query>", "</user_query>",
    "ignore previous", "ignore all previous", "i cannot help", "i can't help",
    "```", "intelligence card",
)

_STOPWORDS = {
    "the", "and", "for", "with", "was", "are", "but", "you", "this", "that",
    "our", "had", "has", "have", "they", "its", "it's", "their", "very", "just",
}


def _content_tokens(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(text.lower()) if len(t) > 2 and t not in _STOPWORDS]


def is_grounded(claim: str, sources: Iterable[str], threshold: float = 0.8) -> bool:
    """True if ≥threshold of the claim's content words appear in any one source."""
    tokens = _content_tokens(claim)
    if not tokens:
        return True
    for src in sources:
        src_tokens = set(_content_tokens(src))
        if src_tokens and sum(t in src_tokens for t in tokens) / len(tokens) >= threshold:
            return True
    return False


def _uses_word(text: str, word: str, allowed_cue: re.Pattern[str] | None = None) -> bool:
    """
    Case-insensitive whole-phrase match that ignores negated uses ("not quiet").
    `allowed_cue` adds extra clause-level cues that also excuse a mention.
    """
    lowered = text.lower()
    for m in re.finditer(rf"\b{re.escape(word.lower())}\b", lowered):
        window = lowered[max(0, m.start() - 100):m.start()]
        breaks = list(_CLAUSE_BREAK_RE.finditer(window))
        clause = window[breaks[-1].end():] if breaks else window
        if _NEGATION_CUE_RE.search(clause) or _DESIRE_AFTER_RE.search(lowered[m.end():]):
            continue
        if allowed_cue is not None and allowed_cue.search(clause):
            continue
        return True
    return False


def _leakage(text: str) -> list[str]:
    lowered = text.lower()
    hits = [m for m in _LEAKAGE_MARKERS if m in lowered]
    if "{" in text or "}" in text:
        hits.append("raw JSON braces")
    return hits


# ─── Intent ───────────────────────────────────────────────────────────────────

_INTENT_STR_LIMITS = {"occasion": 60, "cuisine": 60, "neighborhood": 80, "date": 40}
_CITY_MAX = 80
_LIST_MAX_ITEMS = 10
_LIST_ITEM_MAX = 60
_UNSAFE_CHARS_RE = re.compile(r"[<>{}\n\r`]")


def sanitize_intent(intent: Any) -> tuple[Any, list[str]]:
    """
    Bound every field of a parsed VenueIntent. Intent fields flow into Google
    Places queries, ClickHouse params, cache keys and Senso slugs, so a bloated
    or injected field (e.g. a city of "NYC. Also, ignore...") must not survive.
    Returns (possibly-updated intent, list of fixes applied).
    """
    updates: dict[str, Any] = {}
    fixes: list[str] = []

    city = intent.city or "Unknown"
    if len(city) > _CITY_MAX or _UNSAFE_CHARS_RE.search(city):
        updates["city"] = "Unknown"
        fixes.append("city_reset")

    for name, limit in _INTENT_STR_LIMITS.items():
        val = getattr(intent, name)
        if isinstance(val, str):
            clean = _UNSAFE_CHARS_RE.sub(" ", val).strip()[:limit]
            if clean != val:
                updates[name] = clean or (None if name != "occasion" else "dining")
                fixes.append(f"{name}_truncated")

    for name in ("dietary_restrictions", "other_signals"):
        vals = getattr(intent, name) or []
        clean = [
            _UNSAFE_CHARS_RE.sub(" ", str(v)).strip()[:_LIST_ITEM_MAX]
            for v in vals[:_LIST_MAX_ITEMS]
        ]
        clean = [v for v in clean if v]
        if clean != list(vals):
            updates[name] = clean
            fixes.append(f"{name}_bounded")

    if updates:
        intent = intent.model_copy(update=updates)
    return intent, fixes


# ─── Venue intelligence (why-card) ────────────────────────────────────────────

REQUIRED_BARS = {"ambiance", "privacy", "service", "value", "occasion_fit"}
_MAX_CARD_CHARS = 700
_MAX_SUGGESTION_CHARS = 200
_MAX_LIVE_SIGNAL_CHARS = 200

_LOUD_BANNED = ("intimate", "romantic", "cosy", "cozy", "quiet", "hushed", "tranquil", "serene")
_BUDGET_BANNED = ("upscale", "refined", "romantic", "date night", "fine dining",
                  "fine-dining", "luxurious", "luxury", "white tablecloth")
_ROMANTIC_WORDS = ("romantic", "date night")


def _city_in_address(city: str, address: str) -> bool:
    addr = address.lower()
    if city.lower() in addr:
        return True
    words = [w for w in city.lower().split() if w not in ("city", "the")]
    return bool(words) and all(w in addr for w in words)


def _price_grounded(amount: int, venue_price: int, sources: Iterable[str]) -> bool:
    if venue_price > 0 and abs(amount - venue_price) <= max(5, venue_price * 0.25):
        return True
    return any(re.search(rf"\b{amount}\b", s) for s in sources)


def check_intelligence(intel: Any, venue: Any, intent: Any, query: str = "") -> list[Violation]:
    """Validate one VenueIntelligence against the venue it describes."""
    out: list[Violation] = []
    narrative = f"{intel.why_card}\n{intel.scenario}"
    all_text = "\n".join([narrative, intel.live_signal or "", *intel.suggestions])
    quotes = list(venue.key_quotes or [])
    noise = (venue.noise_level or "").lower()
    price = int(venue.price_per_head or 0)

    # Structure
    if not intel.why_card.strip() or not intel.scenario.strip():
        out.append(Violation("empty_field", "why_card or scenario is empty"))
    if len(intel.why_card) > _MAX_CARD_CHARS or len(intel.scenario) > _MAX_CARD_CHARS:
        out.append(Violation("too_long", "why_card or scenario exceeds length limit"))
    if intel.live_signal and len(intel.live_signal) > _MAX_LIVE_SIGNAL_CHARS:
        out.append(Violation("too_long", "live_signal exceeds length limit"))
    if any(len(s) > _MAX_SUGGESTION_CHARS for s in intel.suggestions):
        out.append(Violation("too_long", "a suggestion exceeds length limit"))
    missing = REQUIRED_BARS - set(intel.sensitivity_bars)
    if missing:
        out.append(Violation("missing_bars", f"missing sensitivity bars: {sorted(missing)}"))
    if len(intel.suggestions) != 4:
        out.append(Violation("suggestion_count", f"expected 4 suggestions, got {len(intel.suggestions)}", "warn"))

    # Safety: prompt leakage, raw JSON, links (possible indirect-injection exfil)
    leaks = _leakage(all_text)
    if leaks:
        out.append(Violation("leakage", f"output contains {leaks}"))
    if _URL_RE.search(all_text):
        out.append(Violation("url_in_output", "output contains a URL"))

    # Tone calibration (mirrors _SYNTHESIS_PROMPT rules)
    if noise in ("loud", "very_loud"):
        bad = [w for w in _LOUD_BANNED if _uses_word(narrative, w)]
        if bad:
            out.append(Violation("tone_mismatch", f"loud venue described as {bad}"))
    if 0 < price < 30:
        bad = [w for w in _BUDGET_BANNED if _uses_word(narrative, w)]
        if bad:
            out.append(Violation("tone_mismatch", f"${price}/head venue described as {bad}"))
    romantic_contradicted = noise in ("moderate", "loud", "very_loud") or 0 < price < 60
    if romantic_contradicted:
        bad = [w for w in _ROMANTIC_WORDS if _uses_word(narrative, w)]
        if bad and not any(v.code == "tone_mismatch" for v in out):
            out.append(Violation("tone_mismatch", f"romantic language for noise={noise or '?'} price=${price}"))

    # Location grounding: the intent city must not leak into cards for venues elsewhere
    city = (intent.city or "").strip()
    address = venue.address or ""
    if city and city != "Unknown" and address and not _city_in_address(city, address):
        if _uses_word(narrative, city, allowed_cue=_OUTSIDE_CUE_RE):
            out.append(Violation("location_ungrounded", f"mentions '{city}' but venue address is '{address}'"))

    # Price grounding: every $ amount in a factual field must match venue price, a
    # quote, or the query. Suggestions are questions ("anything under $50?"), not claims.
    for m in _PRICE_RE.finditer(f"{narrative}\n{intel.live_signal or ''}"):
        amt = int(m.group(1))
        if not _price_grounded(amt, price, [*quotes, query]):
            out.append(Violation("price_ungrounded", f"${amt} not supported by venue data"))
            break

    # Quote grounding: text in quotation marks must come from key_quotes
    for q in re.findall(r"[\"“]([^\"”]{12,})[\"”]", narrative):
        if not is_grounded(q, quotes):
            out.append(Violation("quote_ungrounded", f"quoted text not in key_quotes: {q[:60]!r}"))
            break

    return out


# ─── Published guide ──────────────────────────────────────────────────────────

def check_guide(markdown: str, venues: list[Any]) -> list[Violation]:
    """Validate a generated guide before it is published to Senso (public, AI-indexed)."""
    out: list[Violation] = []
    quotes = [q for v in venues for q in (v.key_quotes or [])]
    prices = [int(v.price_per_head or 0) for v in venues]

    if not markdown.strip():
        return [Violation("empty_field", "guide is empty")]
    if len(markdown.split()) > 900:
        out.append(Violation("too_long", "guide exceeds 900 words", "warn"))

    leaks = [m for m in _LEAKAGE_MARKERS if m in markdown.lower() and m != "```"]
    if leaks:
        out.append(Violation("leakage", f"guide contains {leaks}"))
    if _URL_RE.search(markdown):
        out.append(Violation("url_in_output", "guide contains a URL", "warn"))

    for q in re.findall(r"[\"“]([^\"”]{12,})[\"”]", markdown):
        if not is_grounded(q, quotes):
            out.append(Violation("quote_ungrounded", f"quote not in venue signals: {q[:60]!r}"))

    for m in _PRICE_RE.finditer(markdown):
        amt = int(m.group(1))
        if not any(_price_grounded(amt, p, quotes) for p in prices or [0]):
            out.append(Violation("price_ungrounded", f"${amt} not supported by any venue's data"))

    names_mentioned = sum(1 for v in venues if v.name and v.name.lower() in markdown.lower())
    if venues and names_mentioned == 0:
        out.append(Violation("off_topic", "guide mentions none of the ranked venues"))

    return out


# ─── Signal extraction ────────────────────────────────────────────────────────

def filter_grounded_quotes(quotes: list[str], source_text: str) -> list[str]:
    """Keep only extracted quotes that actually appear (approximately) in the source."""
    return [q for q in quotes if is_grounded(q, [source_text], threshold=0.8)]
