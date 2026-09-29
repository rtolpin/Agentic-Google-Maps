"""
Eval runner for The Right Spot's LLM pipeline.

    cd backend
    python -m evals.run                       # offline suites only — free, deterministic (CI)
    python -m evals.run --live                # + intent & synthesis against real Claude
    python -m evals.run --live --judge        # + LLM-as-judge scoring and judge calibration
    python -m evals.run --suite intent --live --repeats 3
    python -m evals.run --report eval-report.json

Offline suites
  input_guard   prompt-injection recall, false-positive rate, PII redaction
  output_guard  guardrail precision/recall on hand-labelled synthesis outputs
  ranking       occasion re-rank regressions (romantic / upscale / casual)

Live suites (call the Anthropic API — cost money)
  intent            parse_intent field accuracy on the golden set + latency
  synthesis         guardrail pass rate on real why-cards (+ judge scores with --judge)
  judge_calibration judge agreement with the hand labels (requires --judge)

Exit code is 1 if any suite that ran is below its threshold.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from guardrails.input_guard import check_query  # noqa: E402
from guardrails.output_guard import blocking, check_intelligence  # noqa: E402
from models.models import ScoredVenue, VenueIntelligence, VenueIntent  # noqa: E402

from evals.graders import grade_intent, grade_ranking, percentile  # noqa: E402

DATASETS = Path(__file__).parent / "datasets"
OFFLINE_SUITES = ("input_guard", "output_guard", "ranking")
LIVE_SUITES = ("intent", "synthesis")
JUDGE_SUITES = ("judge_calibration",)

THRESHOLDS = {
    "input_guard": 1.0,
    "output_guard": 1.0,
    "ranking": 1.0,
    "intent": 0.90,
    "synthesis": 0.85,
    "synthesis_judge": 0.80,
    "judge_calibration": 0.90,
}

_DEFAULT_BARS = {"ambiance": 50, "privacy": 50, "service": 50, "value": 50, "occasion_fit": 50}
_DEFAULT_SUGGESTIONS = ["Is it busy on weekends?", "Do they take reservations?",
                        "What should I order?", "Is there parking nearby?"]


@dataclass
class SuiteResult:
    name: str
    score: float
    threshold: float
    total: int
    metrics: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold


def _load(name: str) -> dict[str, Any]:
    return json.loads((DATASETS / name).read_text())


def _venue(data: dict[str, Any], city: str = "Unknown") -> ScoredVenue:
    return ScoredVenue(**{"venue_id": data["name"].lower().replace(" ", "_"), "city": city, **data})


def _intel(output: dict[str, Any]) -> VenueIntelligence:
    return VenueIntelligence(**{"sensitivity_bars": _DEFAULT_BARS,
                                "suggestions": _DEFAULT_SUGGESTIONS, **output})


# ─── Offline suites ───────────────────────────────────────────────────────────

def suite_input_guard() -> SuiteResult:
    data = _load("input_guard_cases.json")
    failures: list[dict[str, Any]] = []

    blocked = 0
    for case in data["block"]:
        result = check_query(case["query"])
        if result.allowed:
            failures.append({"id": case["id"], "error": "attack was allowed"})
        else:
            blocked += 1

    allowed = 0
    for case in data["allow"]:
        result = check_query(case["query"])
        if result.allowed:
            allowed += 1
        else:
            failures.append({"id": case["id"], "error": f"benign query blocked ({result.reason})"})

    redacted = 0
    for case in data["redact"]:
        result = check_query(case["query"])
        ok = (result.allowed and sorted(result.redactions) == sorted(case["kinds"])
              and not any(s in result.query for s in case["must_not_contain"]))
        if ok:
            redacted += 1
        else:
            failures.append({"id": case["id"], "error": f"redaction wrong: {result.query!r} {result.redactions}"})

    n_block, n_allow, n_redact = len(data["block"]), len(data["allow"]), len(data["redact"])
    total = n_block + n_allow + n_redact
    return SuiteResult(
        "input_guard", (blocked + allowed + redacted) / total, THRESHOLDS["input_guard"], total,
        metrics={
            "attack_recall": round(blocked / n_block, 3),
            "false_positive_rate": round(1 - allowed / n_allow, 3),
            "redaction_accuracy": round(redacted / n_redact, 3),
        },
        failures=failures,
    )


def suite_output_guard() -> SuiteResult:
    data = _load("synthesis_cases.json")
    failures: list[dict[str, Any]] = []
    tp = fn = tn = fp = 0
    for sc in data["scenarios"]:
        intent = VenueIntent(**sc["intent"])
        venue = _venue(sc["venue"], intent.city)
        for i, lo in enumerate(sc["labelled_outputs"]):
            codes = {v.code for v in blocking(check_intelligence(_intel(lo["output"]), venue, intent))}
            case_id = f"{sc['id']}#{i}"
            if lo["label"] == "bad":
                if codes and set(lo["expect_codes"]) <= codes:
                    tp += 1
                else:
                    fn += 1
                    failures.append({"id": case_id, "error": f"missed: expected {lo['expect_codes']}, got {sorted(codes)}"})
            else:
                if not codes:
                    tn += 1
                else:
                    fp += 1
                    failures.append({"id": case_id, "error": f"false positive: {sorted(codes)}"})
    total = tp + fn + tn + fp
    return SuiteResult(
        "output_guard", (tp + tn) / total, THRESHOLDS["output_guard"], total,
        metrics={
            "recall": round(tp / max(1, tp + fn), 3),
            "precision": round(tp / max(1, tp + fp), 3),
            "false_positive_rate": round(fp / max(1, fp + tn), 3),
        },
        failures=failures,
    )


def suite_ranking() -> SuiteResult:
    from agents.orchestrator import _apply_occasion_rerank

    data = _load("ranking_cases.json")
    failures: list[dict[str, Any]] = []
    passed = 0
    for case in data["cases"]:
        intent = VenueIntent(**case["intent"])
        venues = [_venue(v, intent.city) for v in case["venues"]]
        order = [v.name for v in _apply_occasion_rerank(venues, intent)]
        errs = grade_ranking(order, case["constraints"])
        if errs:
            failures.append({"id": case["id"], "error": "; ".join(errs), "order": order})
        else:
            passed += 1
    total = len(data["cases"])
    return SuiteResult("ranking", passed / total, THRESHOLDS["ranking"], total, failures=failures)


# ─── Live suites ──────────────────────────────────────────────────────────────

class _NoCache:
    """Bypass Redis so evals always exercise the real model."""

    async def get(self, *_: Any) -> None:
        return None

    async def set(self, *_: Any, **__: Any) -> None:
        return None


async def _bounded(coros: list[Callable[[], Awaitable[Any]]], limit: int = 4) -> list[Any]:
    sem = asyncio.Semaphore(limit)

    async def _one(fn: Callable[[], Awaitable[Any]]) -> Any:
        async with sem:
            try:
                return await fn()
            except Exception as exc:  # recorded as a failed case, never aborts the run
                return exc

    return await asyncio.gather(*[_one(c) for c in coros])


async def suite_intent(repeats: int) -> SuiteResult:
    import agents.orchestrator as orch

    orch._cache = _NoCache()
    cases = _load("intent_cases.json")["cases"]
    latencies: list[float] = []

    def _make(case: dict[str, Any]) -> Callable[[], Awaitable[Any]]:
        async def _run() -> Any:
            guard = check_query(case["query"])
            start = time.perf_counter()
            intent = await orch.parse_intent(guard.query)
            latencies.append(time.perf_counter() - start)
            return intent
        return _run

    jobs = [(case, r) for r in range(repeats) for case in cases]
    results = await _bounded([_make(c) for c, _ in jobs])

    failures: list[dict[str, Any]] = []
    passed = 0
    field_hits: dict[str, list[bool]] = {}
    for (case, rep), res in zip(jobs, results):
        if isinstance(res, Exception):
            failures.append({"id": case["id"], "repeat": rep, "error": f"{type(res).__name__}: {res}"})
            continue
        errs = grade_intent(res, case["expect"])
        for f in case["expect"]:
            field_hits.setdefault(f, []).append(not any(e.startswith(f"{f}:") for e in errs))
        if errs:
            failures.append({"id": case["id"], "repeat": rep, "error": "; ".join(errs)})
        else:
            passed += 1

    total = len(jobs)
    return SuiteResult(
        "intent", passed / total, THRESHOLDS["intent"], total,
        metrics={
            "field_accuracy": {f: round(sum(h) / len(h), 3) for f, h in sorted(field_hits.items())},
            "latency_p50_s": round(percentile(latencies, 50), 2),
            "latency_p95_s": round(percentile(latencies, 95), 2),
        },
        failures=failures,
    )


async def suite_synthesis(repeats: int, use_judge: bool) -> list[SuiteResult]:
    from agents.orchestrator import generate_venue_intelligence

    judge = None
    if use_judge:
        from evals.judge import Judge
        judge = Judge()

    scenarios = _load("synthesis_cases.json")["scenarios"]
    jobs = [(sc, r) for r in range(repeats) for sc in scenarios]

    def _make(sc: dict[str, Any]) -> Callable[[], Awaitable[Any]]:
        async def _run() -> Any:
            intent = VenueIntent(**sc["intent"])
            venue = _venue(sc["venue"], intent.city)
            intel = await generate_venue_intelligence(venue, intent)
            violations = check_intelligence(intel, venue, intent)
            verdict = None
            if judge is not None:
                verdict = await judge.grade(sc["intent"], sc["venue"], intel.model_dump(), sc["criteria"])
            return intel, violations, verdict
        return _run

    results = await _bounded([_make(sc) for sc, _ in jobs])

    guard_pass = 0
    guard_failures: list[dict[str, Any]] = []
    judge_pass = judged = 0
    judge_failures: list[dict[str, Any]] = []
    split: dict[str, list[bool]] = {"tuned": [], "held_out": []}
    scores: dict[str, list[int]] = {"faithfulness": [], "tone_fit": [], "helpfulness": []}
    for (sc, rep), res in zip(jobs, results):
        if isinstance(res, Exception):
            guard_failures.append({"id": sc["id"], "repeat": rep, "error": f"{type(res).__name__}: {res}"})
            continue
        intel, violations, verdict = res
        blocked = blocking(violations)
        if blocked:
            guard_failures.append({"id": sc["id"], "repeat": rep,
                                   "error": "; ".join(f"{v.code}: {v.detail}" for v in blocked),
                                   "why_card": intel.why_card})
        else:
            guard_pass += 1
        if verdict is not None:
            judged += 1
            for k in scores:
                scores[k].append(getattr(verdict, k))
            split["held_out" if sc.get("held_out") else "tuned"].append(verdict.overall_pass)
            if verdict.overall_pass:
                judge_pass += 1
            else:
                judge_failures.append({"id": sc["id"], "repeat": rep, "error": verdict.rationale,
                                       "why_card": intel.why_card})

    total = len(jobs)
    out = [SuiteResult("synthesis", guard_pass / total, THRESHOLDS["synthesis"], total,
                       metrics={"guardrail_pass_rate": round(guard_pass / total, 3)},
                       failures=guard_failures)]
    if judge is not None:
        out.append(SuiteResult(
            "synthesis_judge", judge_pass / max(1, total), THRESHOLDS["synthesis_judge"], total,
            metrics={"judged": judged,
                     **{f"mean_{k}": round(sum(v) / len(v), 2) for k, v in scores.items() if v},
                     **{f"pass_rate_{k}": round(sum(v) / len(v), 3) for k, v in split.items() if v}},
            failures=judge_failures,
        ))
    return out


async def suite_judge_calibration() -> SuiteResult:
    from evals.judge import Judge

    judge = Judge()
    scenarios = _load("synthesis_cases.json")["scenarios"]
    jobs = [(sc, i, lo) for sc in scenarios for i, lo in enumerate(sc["labelled_outputs"])]

    def _make(sc: dict[str, Any], lo: dict[str, Any]) -> Callable[[], Awaitable[Any]]:
        async def _run() -> Any:
            output = _intel(lo["output"]).model_dump()
            return await judge.grade(sc["intent"], sc["venue"], output, sc["criteria"])
        return _run

    results = await _bounded([_make(sc, lo) for sc, _, lo in jobs])
    agree = 0
    failures: list[dict[str, Any]] = []
    for (sc, i, lo), verdict in zip(jobs, results):
        case_id = f"{sc['id']}#{i}"
        if isinstance(verdict, Exception) or verdict is None:
            failures.append({"id": case_id, "error": f"no verdict: {verdict!r}"})
            continue
        if verdict.overall_pass == (lo["label"] == "good"):
            agree += 1
        else:
            failures.append({"id": case_id, "error": f"label={lo['label']} judge_pass={verdict.overall_pass}: {verdict.rationale}"})
    total = len(jobs)
    return SuiteResult("judge_calibration", agree / total, THRESHOLDS["judge_calibration"], total,
                       metrics={"agreement": round(agree / total, 3)}, failures=failures)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def run_offline() -> list[SuiteResult]:
    return [suite_input_guard(), suite_output_guard(), suite_ranking()]


async def _run(args: argparse.Namespace) -> list[SuiteResult]:
    wanted = set(args.suite or [])
    if not wanted:
        wanted = set(OFFLINE_SUITES)
        if args.live:
            wanted |= set(LIVE_SUITES)
        if args.judge:
            wanted |= set(JUDGE_SUITES)
    if wanted & set(LIVE_SUITES + JUDGE_SUITES) and not args.live:
        sys.exit("error: live suites call the Anthropic API — pass --live to confirm")
    if "judge_calibration" in wanted and not args.judge:
        sys.exit("error: judge_calibration requires --judge")

    results: list[SuiteResult] = []
    offline = {"input_guard": suite_input_guard, "output_guard": suite_output_guard, "ranking": suite_ranking}
    for name in OFFLINE_SUITES:
        if name in wanted:
            results.append(offline[name]())
    if "intent" in wanted:
        results.append(await suite_intent(args.repeats))
    if "synthesis" in wanted:
        results.extend(await suite_synthesis(args.repeats, args.judge))
    if "judge_calibration" in wanted:
        results.append(await suite_judge_calibration())
    return results


def _print(results: list[SuiteResult], verbose: bool) -> None:
    print(f"\n{'suite':<20}{'score':>8}{'threshold':>11}{'cases':>7}  status")
    print("─" * 56)
    for r in results:
        print(f"{r.name:<20}{r.score:>8.1%}{r.threshold:>11.0%}{r.total:>7}  {'PASS' if r.passed else 'FAIL'}")
    for r in results:
        if r.metrics:
            print(f"\n{r.name} metrics: {json.dumps(r.metrics)}")
        shown = r.failures if verbose else r.failures[:5]
        for f in shown:
            print(f"  ✗ {f.get('id')}: {f.get('error')}")
        if len(r.failures) > len(shown):
            print(f"  … {len(r.failures) - len(shown)} more (use --verbose)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", action="append",
                        choices=[*OFFLINE_SUITES, *LIVE_SUITES, *JUDGE_SUITES],
                        help="run only this suite (repeatable)")
    parser.add_argument("--live", action="store_true", help="enable suites that call the Anthropic API")
    parser.add_argument("--judge", action="store_true", help="add LLM-as-judge scoring (implies extra API cost)")
    parser.add_argument("--repeats", type=int, default=1, help="repeat live cases N times to measure variance")
    parser.add_argument("--report", type=Path, help="write a JSON report to this path")
    parser.add_argument("--verbose", action="store_true", help="show every failure")
    args = parser.parse_args(argv)

    if args.live:
        try:
            from dotenv import load_dotenv
            load_dotenv(_BACKEND.parent / ".env")
        except ImportError:
            pass

    results = asyncio.run(_run(args))
    _print(results, args.verbose)
    if args.report:
        args.report.write_text(json.dumps(
            [{**asdict(r), "passed": r.passed} for r in results], indent=2, default=str))
        print(f"\nreport written to {args.report}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
