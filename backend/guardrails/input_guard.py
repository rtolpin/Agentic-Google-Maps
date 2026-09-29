"""
Input guardrails — run on every user query before it reaches an LLM.

  1. SANITIZE  — Unicode-normalize, strip control / zero-width characters
                 (a common prompt-smuggling vector), collapse whitespace.
  2. DETECT    — block prompt-injection and jailbreak attempts. Patterns are
                 deliberately narrow so ordinary venue searches never trip them
                 ("ignore the crowds", "System of a Down concert" pass).
  3. REDACT    — replace emails, phone numbers, card numbers and SSNs with
                 placeholders so PII never reaches Claude, Redis, ClickHouse,
                 Datadog tags, or a published Senso guide.

Everything here is pure and synchronous (<1 ms) so it adds no latency.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MIN_QUERY_CHARS = 3
MAX_QUERY_CHARS = 500

BLOCKED_MESSAGE = (
    "That request couldn't be processed. Try describing the kind of place "
    "you're looking for — e.g. \"quiet cafe for deep work in SoHo\"."
)

# Zero-width and bidi-control characters used to hide instructions in text.
_INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(
        r"\b(ignore|disregard|forget|override|bypass|skip)\s+(?:all\s+|any\s+)?(?:of\s+)?"
        r"(?:the\s+|your\s+|my\s+|these\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|original|initial|developer)\s+"
        r"(?:instructions?|prompts?|rules|directions|messages?|context|guidelines)",
        re.IGNORECASE,
    )),
    ("override_instructions", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\s+(?:all\s+)?(?:your|the)\s+"
        r"(?:instructions?|system\s+prompt|prompts?|guidelines|programming)\b",
        re.IGNORECASE,
    )),
    ("prompt_extraction", re.compile(
        r"\b(reveal|show|print|repeat|output|leak|display|tell\s+me|what\s+(?:is|are))\s+"
        r"(?:me\s+)?(?:your|the)\s+(?:full\s+|exact\s+|hidden\s+|initial\s+|original\s+)?"
        r"(?:system\s+prompt|system\s+message|instructions|prompt|hidden\s+rules)",
        re.IGNORECASE,
    )),
    ("role_hijack", re.compile(
        r"\b(you\s+are\s+now|from\s+now\s+on\s+you|pretend\s+(?:to\s+be|you\s+are)|"
        r"new\s+instructions\s*:|act\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered|jailbroken|different)\s)",
        re.IGNORECASE,
    )),
    ("jailbreak", re.compile(
        r"\b(jailbreak|DAN\s+mode|developer\s+mode|do\s+anything\s+now|god\s+mode)\b",
        re.IGNORECASE,
    )),
    ("role_markup", re.compile(
        r"(</?\s*(?:system|assistant|user_query|instructions?)\s*>|\[/?INST\]|<\|im_(?:start|end)\|>|"
        r"(?:^|\n)\s*(?:system|assistant|human)\s*:)",
        re.IGNORECASE,
    )),
]

# PII — ordered so that longer / more specific patterns win.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RUN_RE = re.compile(r"\d(?:[ -]?\d){12,}")  # any run of ≥13 digits with optional separators
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")


@dataclass
class InputGuardResult:
    allowed: bool
    query: str                       # sanitized + redacted query (safe to forward)
    reason: str | None = None        # machine-readable block reason
    redactions: list[str] = field(default_factory=list)

    @property
    def user_message(self) -> str:
        return BLOCKED_MESSAGE if not self.allowed else ""


def sanitize(text: str) -> str:
    """Normalize Unicode and strip invisible / control characters."""
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    text = _CONTROL_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def detect_injection(text: str) -> str | None:
    """Return the matched injection category, or None if the text looks benign."""
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Replace PII with typed placeholders. Returns (redacted_text, kinds_found)."""
    found: list[str] = []

    def _sub(pattern: re.Pattern[str], label: str, s: str) -> str:
        def _repl(m: re.Match[str]) -> str:
            found.append(label.lower())
            return f"[{label}]"
        return pattern.sub(_repl, s)

    def _cards(m: re.Match[str]) -> str:
        # A digit run can hold a card next to other numbers ("555-0142 4111 1111 …"),
        # so test every contiguous span of digit groups, not just the whole run.
        run = m.group(0)
        groups = [g for g in re.finditer(r"\d+", run)]
        for i in range(len(groups)):
            for j in range(len(groups) - 1, i - 1, -1):
                span = run[groups[i].start():groups[j].end()]
                digits = re.sub(r"\D", "", span)
                if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                    found.append("card")
                    return run[:groups[i].start()] + "[CARD]" + _cards_tail(run[groups[j].end():])
        return run

    def _cards_tail(rest: str) -> str:
        return _CARD_RUN_RE.sub(_cards, rest)

    # Phones before cards: the phone pattern can't start or end inside a longer
    # digit run, so it never splits a card, and removing it first keeps a phone
    # number from merging into an adjacent card's digit run.
    text = _sub(_EMAIL_RE, "EMAIL", text)
    text = _sub(_SSN_RE, "SSN", text)
    text = _sub(_PHONE_RE, "PHONE", text)
    text = _CARD_RUN_RE.sub(_cards, text)
    return text, found


def check_query(query: str) -> InputGuardResult:
    """Run the full input pipeline. Callers must forward `result.query`, not the original."""
    cleaned = sanitize(query or "")
    if len(cleaned) < MIN_QUERY_CHARS:
        return InputGuardResult(allowed=False, query=cleaned, reason="too_short")
    if len(cleaned) > MAX_QUERY_CHARS:
        return InputGuardResult(allowed=False, query=cleaned[:MAX_QUERY_CHARS], reason="too_long")

    injection = detect_injection(cleaned)
    if injection:
        return InputGuardResult(allowed=False, query=cleaned, reason=f"prompt_injection:{injection}")

    redacted, kinds = redact_pii(cleaned)
    return InputGuardResult(allowed=True, query=redacted, redactions=kinds)
