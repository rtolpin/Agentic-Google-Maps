"""
Deterministic graders shared by the eval suites. No I/O, no LLM calls.
"""
from __future__ import annotations

from typing import Any


def _norm(v: Any) -> Any:
    """Enums → their value; strings → lowercase."""
    v = getattr(v, "value", v)
    return v.lower() if isinstance(v, str) else v


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(str(_norm(x)) for x in v)
    return str(_norm(v))


def match(value: Any, matcher: dict[str, Any]) -> bool:
    """Apply one matcher ({op: expected}) to a field value."""
    (op, expected), = matcher.items()
    v = _norm(value)
    if op == "eq":
        return v == _norm(expected)
    if op == "in":
        return v in [_norm(e) for e in expected]
    if op == "contains":
        return _norm(expected) in _as_text(value)
    if op == "contains_any":
        text = _as_text(value)
        return any(_norm(e) in text for e in expected)
    if op == "is_null":
        return (v is None) == bool(expected)
    if op == "not_null":
        return (v is not None) == bool(expected)
    if op == "gte":
        return v is not None and v >= expected
    if op == "lte":
        return v is not None and v <= expected
    raise ValueError(f"unknown matcher op: {op}")


def grade_intent(intent: Any, expect: dict[str, dict[str, Any]]) -> list[str]:
    """Return a list of human-readable field failures (empty list = pass)."""
    failures: list[str] = []
    for field, matcher in expect.items():
        if field == "signals":
            value = [intent.occasion, intent.cuisine, *(intent.other_signals or [])]
        else:
            value = getattr(intent, field)
        if not match(value, matcher):
            failures.append(f"{field}: got {_norm(value)!r}, expected {matcher}")
    return failures


def grade_ranking(names_in_order: list[str], constraints: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if "top" in constraints and (not names_in_order or names_in_order[0] != constraints["top"]):
        failures.append(f"top: got {names_in_order[:1]}, expected {constraints['top']!r}")
    if "above" in constraints:
        a, b = constraints["above"]
        if names_in_order.index(a) > names_in_order.index(b):
            failures.append(f"above: {a!r} ranked below {b!r}")
    if "not_in_top" in constraints:
        name, k = constraints["not_in_top"]["name"], constraints["not_in_top"]["k"]
        if name in names_in_order[:k]:
            failures.append(f"not_in_top: {name!r} is in top {k}")
    return failures


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]
