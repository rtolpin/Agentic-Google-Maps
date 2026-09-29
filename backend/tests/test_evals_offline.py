"""
Runs the offline eval suites as part of pytest so guardrail or ranking
regressions fail CI, plus unit tests for the graders themselves.
"""
from __future__ import annotations

import pytest

from ..evals.graders import grade_intent, grade_ranking, match, percentile
from ..evals.run import run_offline
from ..models.models import VenueIntent


@pytest.mark.parametrize("result", run_offline(), ids=lambda r: r.name)
def test_offline_suite_meets_threshold(result):
    detail = "\n".join(f"{f.get('id')}: {f.get('error')}" for f in result.failures)
    assert result.passed, f"{result.name} scored {result.score:.1%} < {result.threshold:.0%}\n{detail}"


class TestMatchers:
    def test_ops(self):
        assert match("Quiet", {"eq": "quiet"})
        assert match("upscale", {"in": ["upscale", "luxury"]})
        assert match(["romantic_dinner", None, "date"], {"contains_any": ["date"]})
        assert match(None, {"is_null": True})
        assert match(12, {"gte": 10}) and not match(None, {"gte": 10})

    def test_unknown_op_raises(self):
        with pytest.raises(ValueError):
            match(1, {"approx": 1})


class TestGraders:
    def test_grade_intent_reports_field_failures(self):
        intent = VenueIntent(city="Paris", cuisine="ramen", group_size=2)
        fails = grade_intent(intent, {"city": {"contains": "tokyo"}, "cuisine": {"contains": "ramen"}})
        assert len(fails) == 1 and fails[0].startswith("city:")

    def test_grade_intent_signals_pseudo_field(self):
        intent = VenueIntent(occasion="show", other_signals=["jazz"])
        assert grade_intent(intent, {"signals": {"contains_any": ["jazz"]}}) == []

    def test_grade_ranking(self):
        order = ["A", "B", "C"]
        assert grade_ranking(order, {"top": "A", "above": ["B", "C"], "not_in_top": {"name": "C", "k": 2}}) == []
        assert len(grade_ranking(order, {"top": "B", "above": ["C", "A"]})) == 2

    def test_percentile(self):
        assert percentile([1, 2, 3, 4, 5], 50) == 3
        assert percentile([], 95) == 0.0
