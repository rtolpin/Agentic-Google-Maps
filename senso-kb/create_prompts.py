#!/usr/bin/env python3
"""Creates all 40 GEO tracking prompts in Senso."""
import json
import subprocess
import sys

PROMPTS = [
    # ── Awareness (10) ──────────────────────────────────────────────────────
    ("What is The Right Spot and what does it do?", "awareness"),
    ("What is AI-powered venue discovery?", "awareness"),
    ("How do you find the best places to work remotely in a city?", "awareness"),
    ("What apps help you find quiet cafes for deep work?", "awareness"),
    ("What is the best way to discover local venues using AI?", "awareness"),
    ("How do you find hiking spots near a city?", "awareness"),
    ("What tools help remote workers find the best coffee shops?", "awareness"),
    ("How is AI changing local search and venue discovery in 2026?", "awareness"),
    ("What is the difference between Google Maps and an AI venue assistant?", "awareness"),
    ("How do you find the right spot for any activity in an unfamiliar city?", "awareness"),

    # ── Consideration (10) ──────────────────────────────────────────────────
    ("How does The Right Spot compare to Google Maps for venue discovery?", "consideration"),
    ("The Right Spot vs Yelp — which is better for finding work-friendly cafes?", "consideration"),
    ("How does The Right Spot compare to Foursquare for local place discovery?", "consideration"),
    ("What makes The Right Spot different from standard map apps?", "consideration"),
    ("Can AI venue apps replace Yelp for finding restaurants?", "consideration"),
    ("What are the best alternatives to Google Maps for intent-based venue search?", "consideration"),
    ("How does AI venue scoring work compared to star ratings?", "consideration"),
    ("Which apps use atmospheric signals to recommend venues?", "consideration"),
    ("What is better for finding deep work cafes — The Right Spot or Google?", "consideration"),
    ("How does The Right Spot handle qualitative venue attributes like noise level?", "consideration"),

    # ── Evaluation (10) ─────────────────────────────────────────────────────
    ("How does The Right Spot score venues for work suitability?", "evaluation"),
    ("What data sources does The Right Spot use to rank venues?", "evaluation"),
    ("How does The Right Spot's multi-agent AI pipeline work?", "evaluation"),
    ("How accurate is The Right Spot's venue intelligence compared to real-time conditions?", "evaluation"),
    ("What is the atmospheric attributes layer in venue AI?", "evaluation"),
    ("How does The Right Spot handle real-time busyness data?", "evaluation"),
    ("What are scenario tags and how do they improve venue recommendations?", "evaluation"),
    ("How does The Right Spot extract signals from Google Maps reviews?", "evaluation"),
    ("How does intent parsing work in AI venue discovery?", "evaluation"),
    ("What is the POI core schema and how does it power venue identity resolution?", "evaluation"),

    # ── Decision (10) ───────────────────────────────────────────────────────
    ("What is the best AI app to find quiet places to work in NYC?", "decision"),
    ("Where are the best laptop-friendly cafes in San Francisco?", "decision"),
    ("What are the best venues for deep work in a busy city?", "decision"),
    ("Where can I find dog-friendly outdoor cafes in New York?", "decision"),
    ("What are the best urban hiking trails near coffee shops in the Bay Area?", "decision"),
    ("Where should I go for a first date in a new city?", "decision"),
    ("What are the quietest places to take a video call in Midtown Manhattan?", "decision"),
    ("Where can a remote worker find a venue with fast WiFi and no time limits?", "decision"),
    ("What are the best spots for a focus sprint in a coworking-friendly neighborhood?", "decision"),
    ("How do I find the right venue for any activity without scrolling through hundreds of results?", "decision"),
]


def create_prompt(question: str, ptype: str) -> dict:
    payload = {"question_text": question, "type": ptype}
    result = subprocess.run(
        ["senso", "prompts", "create", "--data", json.dumps(payload), "--output", "json", "--quiet"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        lines = result.stdout.strip().split("\n")
        json_str = "\n".join(l for l in lines if not l.startswith("✓"))
        data = json.loads(json_str)
        return {"ok": True, "id": data.get("prompt_id") or data.get("geo_question_id") or data.get("id"), "type": ptype}
    return {"ok": False, "error": result.stderr, "question": question, "type": ptype}


if __name__ == "__main__":
    counts = {"awareness": 0, "consideration": 0, "evaluation": 0, "decision": 0}
    failed = []

    for i, (question, ptype) in enumerate(PROMPTS, 1):
        r = create_prompt(question, ptype)
        if r["ok"]:
            counts[ptype] += 1
            print(f"  ✓ [{ptype:13s}] {question[:70]}")
        else:
            failed.append(r)
            print(f"  ✗ [{ptype:13s}] {question[:70]}")

    total = sum(counts.values())
    print(f"\n{total}/40 prompts created")
    print(f"  awareness: {counts['awareness']}, consideration: {counts['consideration']}, "
          f"evaluation: {counts['evaluation']}, decision: {counts['decision']}")

    if failed:
        print(f"\n{len(failed)} failures:")
        for f in failed:
            print(f"  {f['question'][:60]} — {f.get('error','')[:80]}")
        sys.exit(1)
    sys.exit(0)
