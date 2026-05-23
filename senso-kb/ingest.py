#!/usr/bin/env python3
"""Ingests all KB documents into the Senso knowledge base via CLI."""
import json
import subprocess
import sys

FOLDER_IDS = {
    "basemap-geometry":      "873e9bc4-01b8-4be6-914a-3ae58d65ae0c",
    "poi-core-schema":       "62fd50a6-726f-49d6-9ccc-5cadf44d892b",
    "dynamic-layers":        "ef3acd54-92ff-4181-aa7d-017c705e2882",
    "atmospheric-attributes":"c3f815f8-b947-41fd-9127-3a76b16f5b74",
    "reviews-sentiment":     "6f90c892-4d92-4aa5-aabd-d56ec664bc5f",
    "spatial-curations":     "36ca943a-898b-4c11-a315-631e594cc463",
    "agent-routing-rules":   "35d02378-8394-45b2-bb9f-fca4bb5bbbe1",
    "build-logs":            "12c1111a-8542-4f0b-b3f8-c0e173255533",
}

DOCUMENTS = [
    {
        "folder": "basemap-geometry",
        "title": "2026-05-23 - The Right Spot Product Overview and Mission",
        "text": """Source: Internal — agentic-engineering-hack codebase

# The Right Spot — Product Overview

## What It Is

The Right Spot is an AI-powered venue intelligence platform that transforms Google Maps from a navigation tool into a conversational discovery assistant. Instead of returning a list of pins when you search "quiet cafe," it understands intent — and returns the right spot for your specific situation, time, and mood.

## The Problem It Solves

Google Maps, Yelp, and Foursquare all answer "what is near me." They don't answer "what is near me and is quiet on a Tuesday morning, has fast WiFi, allows you to stay three hours without being asked to leave, and has window seating." The gap between category search and intent search is where The Right Spot lives.

## How It Works

When a user types a query — "places to read a book in the Mission," "offices to scout near Midtown," "post-hike coffee with a view" — the platform:

1. Parses intent using Claude (Anthropic) to extract city, category, scenario, and atmospheric requirements
2. Scrapes venue signals via Nimble SERP API (Google Maps extraction + review snippets)
3. Scores venues in ClickHouse using a multi-factor algorithm: rating, review consensus, atmospheric attribute match, crowd signal
4. Synthesizes recommendations using Claude with prompt caching for consistent brand voice
5. Streams results via FastAPI Server-Sent Events to the frontend in real time
6. Displays on Google Maps using AdvancedMarkerElement pins with a conversational overlay UI

## Who It Serves

- Remote workers and freelancers looking for productive third places
- Urban explorers discovering new neighborhoods
- Travelers landing in an unfamiliar city who want local intelligence fast
- Anyone who wants a smarter answer than "here are 47 cafes with 4+ stars"

## Key Differentiators

- Intent-first search: matches atmospheric attributes and scenario tags, not just category
- Multi-agent AI pipeline: Orchestrator → ScraperAgent + ValidatorAgent + GlobalIntelligenceAgent in parallel
- Real-time data: Nimble SERP pulls live Google Maps data, not a stale database
- Conversational map UI: follow-up chips, AI overlay, venue detail sidebar with sensitivity bars
- Datadog APM tracing: every agent call, DB query, and API hit is traced end-to-end
- Senso GEO publishing: venue intelligence published as AI-citable content

## Tech Stack

- Backend: FastAPI + Python 3.11, AsyncAnthropic, Pydantic v2
- Data: ClickHouse MergeTree (venue_signals, city_benchmarks, user_sessions), Redis caching
- AI: Claude claude-sonnet-4-6 with prompt caching (cache_control: ephemeral)
- APIs: Nimble SERP (Google Maps extraction), Google Maps Platform (display), Senso.ai (GEO)
- Observability: Datadog APM with distributed tracing across all agents
- Frontend: Next.js, Google Maps JS API v3 beta, AdvancedMarkerElement, SSE streaming
""",
    },
    {
        "folder": "basemap-geometry",
        "title": "2026-05-23 - Geospatial Architecture — Layers and Data Model",
        "text": """Source: Internal — agentic-engineering-hack architecture docs

# Geospatial Architecture — The Right Spot Data Layers

The Right Spot uses a multi-tiered geospatial architecture that mirrors Google Maps' own data separation: physical map traits, human intent, and contextual live data are kept in distinct layers.

## Layer 1: Base Map Features (basemap-geometry)

Core geographic and administrative data. Neighborhood polygon boundaries define sub-city search scopes. Street layouts inform walk-time calculations. Public transit nodes feed the transit_proximity_minutes field in the Atmospheric Attributes layer. Architectural properties (rooftop access, indoor/outdoor flow) are captured as binary flags.

**Why separate:** Structural precision queries ("coffee shops within a 5-minute walk of the Flatiron building") require clean spatial data that isn't contaminated by user sentiment. Geohash at precision 7 (~150m accuracy) enables fast ClickHouse bounding-box lookups without trigonometric calculations.

## Layer 2: POI Core Schema (poi-core-schema)

Foundational taxonomy definitions for every venue type. Maps directly to Google Places API Primary Types. Stores: place_id, name, coordinates, primary_type, address, hours, ratings, spatial enrichment.

**Google TOS compliance:** contact fields (phone, website, google_maps_uri) are display-only — never stored in ClickHouse. place_id is the only persistent Google identifier. All other Place Details are fetched fresh at display time.

## Layer 3: Dynamic Environmental Layers (dynamic-layers)

Real-time variables that change a venue's environment: peak busyness by hour (from Google Popular Times via Nimble SERP), live traffic delays, active community events, construction closures. Updated on each search query — never cached beyond the Redis TTL.

## Layer 4: Atmospheric Attributes (atmospheric-attributes)

Qualitative contextual tags extracted from review text by Claude. Work suitability, ambiance, accessibility, outdoor quality, time sensitivity, scenario tags. The layer that answers "good for deep work" versus "packed by noon."

## Layer 5: Review Sentiment (reviews-sentiment)

Aggregated review text distilled into high-confidence consensus points. AI-generated summaries highlighting recurring pros and cons. Example: "Users consistently say the WiFi is fast but seating limited after 2 PM." Updated weekly per venue.

## Layer 6: Spatial Curations (spatial-curations)

Human-curated clusters and editorial narratives. Neighborhood guides, micro-collections, localized listicles. Long-tail intent capture: "best laptop-friendly spaces during NYC Tech Week."

## Layer 7: Agent Routing Rules (agent-routing-rules)

The application's proprietary logic layer. Prompt weights, search ranking algorithms, guidelines for answering questions accurately. Defines how raw database data gets formatted into conversational responses.

## ClickHouse Schema

Three MergeTree tables:
- venue_signals (ReplacingMergeTree): venue_id, place_id, name, city, coordinates, rating, review_count, match_score, atmospheric signals, updated_at
- city_benchmarks (AggregatingMergeTree): city-level aggregates for relative scoring
- user_sessions (MergeTree): query intent, city, category, result_count for analytics
""",
    },
    {
        "folder": "dynamic-layers",
        "title": "2026-05-23 - Dynamic Environmental Layers — Real-Time Venue Signals",
        "text": """Source: Internal — agentic-engineering-hack/backend/agents/scraper_agent.py

# Dynamic Environmental Layers

## What They Are

Dynamic layers are the real-time, situational variables that change a venue's suitability moment to moment. A cafe that's perfect for deep work at 9 AM may be unusable at noon. A park that's serene on a weekday becomes crowded on Saturday. Dynamic layers capture this temporal variance.

## Data Sources

### Nimble SERP API (google_maps engine)
Pulls live Google Maps data for each search query:
- place_id and coordinates for identity resolution
- Current open/closed status
- Popular times data (busyness by hour, day of week)
- Recent review snippets (last 30 days)
- Photo count as a proxy for venue activity

### Nimble SERP API (google engine)
Pulls web results for review snippets and contextual mentions:
- Review text from Google, Yelp, TripAdvisor cross-references
- News mentions for events or closures
- Blog mentions for local editorial coverage

## Signal Types

| Signal | Source | Update Frequency | Use |
|--------|--------|-----------------|-----|
| open_now | Nimble Maps | Per query | Filter out closed venues |
| busyness_now | Popular Times | Per query | "Right now" queries |
| peak_hours | Popular Times | Per query | Best time recommendations |
| recent_reviews | Nimble SERP | Per query | Fresh sentiment signals |
| event_mentions | Web search | Per query | "Tech Week," "festival" context |

## Redis Caching Strategy

Dynamic signals are cached in Redis with TTLs calibrated to data volatility:
- open_now: 15-minute TTL (changes at opening/closing time)
- busyness_now: 30-minute TTL (popular times update hourly)
- recent_reviews: 24-hour TTL (review cadence is daily, not hourly)
- Venue identity data (place_id, coordinates): 7-day TTL (rarely changes)

## How It Feeds Into Scoring

The ClickHouse scoring algorithm weights dynamic signals alongside stable signals:
- 40% weight: rating + review_count (stable)
- 30% weight: atmospheric attribute match (stable)
- 20% weight: busyness score (dynamic — inverted: less busy = higher score for work queries)
- 10% weight: recency of positive reviews (dynamic)

## Situational Query Examples

"Where is a quiet spot to take a call in Gramercy right now that isn't packed?"
→ Dynamic layer: busyness_now < 60% + open_now: true + atmospheric: good_for_calls: true

"Best coffee shop on a rainy Wednesday afternoon"
→ Dynamic: weather_impact flag + best_days includes wednesday + has_outdoor_seating: false (or indoor preference)
""",
    },
    {
        "folder": "reviews-sentiment",
        "title": "2026-05-23 - Review Sentiment Methodology — How The Right Spot Processes Reviews",
        "text": """Source: Internal — agentic-engineering-hack/backend/agents/scraper_agent.py + orchestrator.py

# Review Sentiment Methodology

## The Problem With Raw Reviews

A venue with 847 Google reviews cannot be summarized by reading all 847. And a 4.3-star rating tells you nothing about whether the WiFi drops out, whether the barista is friendly to laptop workers, or whether it gets so loud by 11 AM that calls become impossible.

The Right Spot processes reviews differently: it uses Claude to extract structured atmospheric signals from review text, then builds consensus points from across multiple signals.

## The Extraction Pipeline

### Step 1: Snippet Collection (Nimble SERP)
For each venue, Nimble SERP pulls the most relevant recent review snippets from Google Maps search results — typically the 3-5 snippets Google surfaces for a query. These are the reviews Google's own relevance ranking considers most useful for the search context.

### Step 2: Signal Extraction (Claude)
Claude's `extract_venue_signals` function processes each snippet and extracts:
- Atmospheric attribute flags (wifi_quality, noise_level, seating_comfort, etc.)
- Scenario tags (solo_work_session, first_date, dog_walk_destination)
- Temporal signals (best_time_of_day, avoid_during)
- Confidence level per extracted signal (how explicit vs. inferred)

Example input: "Perfect for working — outlets everywhere, fast wifi, and staff don't rush you even if you're there for 4 hours."
Example output: {wifi_quality: "excellent", power_outlets: "abundant", time_limit_enforced: false, scenario_tags: ["long_stay_nomad", "solo_work_session"]}

### Step 3: Consensus Building (ClickHouse)
Multiple signals for the same venue get aggregated in ClickHouse:
- ReplacingMergeTree deduplicates: latest signal per (venue_id, attribute) wins
- Confidence score = (sum of signal weights) / (count of independent sources)
- Attributes with confidence < 0.3 are flagged as uncertain and excluded from recommendations

### Step 4: Synthesis (Claude)
When generating a recommendation, Claude synthesizes the structured attributes into natural language that matches the brand voice: specific, honest, time-aware.

## Consensus Points Format

Each venue in the KB stores up to 5 consensus points in this format:
- [Positive consensus]: "WiFi reliably fast even during peak hours — multiple reviewers note 50+ Mbps"
- [Negative consensus]: "Seating fills up after 11 AM on weekdays — arrive early or expect to wait"
- [Conditional]: "Quiet in the morning, significantly louder after the lunch rush"

## Freshness

Review signals are refreshed whenever a user searches for a venue:
- If cached data is < 24 hours old: use cached
- If cached data is 24 hours - 7 days old: use cached, schedule background refresh
- If cached data is > 7 days old: refresh synchronously before returning result
""",
    },
    {
        "folder": "spatial-curations",
        "title": "2026-05-23 - Spatial Curation Guide — NYC Deep Work Spots",
        "text": """Source: Internal editorial + atmospheric attributes KB

# Best Deep Work Venues in New York City

A curated guide to NYC venues that consistently score high on work suitability — based on WiFi quality, power outlet abundance, noise consistency, and time-limit tolerance. Each venue includes a best-time window and one honest caveat.

## SoHo / NoLita

**Cafe Integral (Nolita)**
Best for: solo focus sprints, pour-over specialists
Best time: weekdays 8–11 AM — gets social by noon
Caveat: No food menu, just coffee — plan for a 2-hour maximum before hunger sets in
Atmospheric score: WiFi excellent, outlets moderate, noise quiet → moderate

**Housing Works Bookstore Cafe**
Best for: reading marathons, long stays, laptop work with a literary backdrop
Best time: weekdays any time; avoid Saturdays 2–6 PM (events)
Caveat: WiFi speed is inconsistent — bring a hotspot for anything bandwidth-intensive
Atmospheric score: WiFi moderate, outlets scarce, noise quiet

## Midtown

**New York Public Library (Main Branch)**
Best for: sustained deep work, zero noise tolerance required, academic atmosphere
Best time: weekdays 10 AM – 4 PM; avoid school holidays
Caveat: No food or drink beyond the lobby cafe; lockers available for bags
Atmospheric score: WiFi fast, outlets moderate, noise silent

## Brooklyn / Williamsburg

**Toby's Estate Coffee (Williamsburg)**
Best for: creative work, aesthetic inspiration, laptop-friendly afternoon sessions
Best time: weekday mornings and early afternoons
Caveat: Can get crowded on weekends; communal tables mean noise varies by neighbors
Atmospheric score: WiFi excellent, outlets abundant, noise moderate

## Transit Note
All four venues are within a 10-minute walk of multiple subway lines. NYPL is at 5th Ave/42nd St (B/D/F/M + 4/5/6 nearby). Housing Works at Spring St (C/E, 6). Toby's at Bedford Ave (L).

---
*Powered by Senso — your AI-searchable knowledge base.*
""",
    },
    {
        "folder": "spatial-curations",
        "title": "2026-05-23 - Spatial Curation Guide — SF Bay Area Hiking with Post-Hike Coffee",
        "text": """Source: Internal editorial + atmospheric attributes KB

# Urban Hiking + Post-Hike Coffee in San Francisco

A curated pairing guide: trail access points matched to nearby venues that score high on post-hike recovery — food menu, dog-friendly, outdoor seating, and proximity to trail endpoints.

## Twin Peaks / Upper Market

**Trail:** Twin Peaks Summit Trail (2.5 miles, easy-moderate)
**Post-hike:** Sightglass Coffee (SoMa, 15-min drive/transit)
- Best for: specialty coffee recovery, laptop-friendly for post-hike work
- Dog policy: outdoor seating only
- Transit: J-Church from Castro to SoMa area
- Caveat: No direct trail-to-coffee walk — transit required

## Lands End / Sutro

**Trail:** Lands End Trail (3.4 miles, easy) — ocean views, ruins of Sutro Baths
**Post-hike:** Java Beach Cafe (Inner Sunset)
- Best for: dog-friendly, casual, full food menu, post-beach crowd
- Dog policy: outdoor seating + dog-friendly interior
- Best time: weekday mornings before the surf crowd arrives
- Caveat: Parking is difficult; transit from Ocean Beach (N-Judah) is 10 min

## Marin Headlands (Accessible from SF)

**Trail:** Miwok Trail to Hawk Hill (5.1 miles, moderate)
**Post-hike:** Mill Valley's Equator Coffees
- Best for: quieter post-hike venue, away from tourist crowds
- Scenic view: courtyard, mountain backdrop
- Caveat: Requires driving across the Golden Gate — not transit-accessible

## Observation Note
All SF hiking venues scored on the outdoor cluster: trail_access_direct, shade_available, scenic_view, dog_friendly. Post-hike venues scored on: food_menu, dog_friendly, has_outdoor_seating, transit_proximity.

---
*Powered by Senso — your AI-searchable knowledge base.*
""",
    },
    {
        "folder": "agent-routing-rules",
        "title": "2026-05-23 - Agent Routing Rules — Intent Parsing and Query Classification",
        "text": """Source: Internal — agentic-engineering-hack/backend/agents/orchestrator.py

# Agent Routing Rules — Intent Parsing and Query Classification

## Overview

The Right Spot's orchestrator uses Claude to parse user intent from free-form natural language queries into structured search parameters. This document defines how intent is classified, how ambiguous queries are handled, and how the multi-agent pipeline routes based on intent type.

## Intent Parsing Schema

Every query is parsed into a VenueSearchIntent object:
- city: string (extracted or inferred from context)
- category: enum (cafe, park, library, coworking, office, restaurant, bar, hiking_area, etc.)
- scenario_tags: list[str] (from the 30-tag taxonomy)
- atmospheric_requirements: dict (wifi_quality, noise_level, etc.)
- time_context: optional (morning, evening, right_now)
- group_size: optional (solo, couple, small_group, team)

## Query Classification Rules

### Rule 1: Scenario-First Resolution
If the query contains a scenario keyword, prioritize scenario tag matching over category.

Examples:
- "places to read a book" → scenario: reading_marathon, category: [library, cafe, bookstore]
- "quiet spot for a call" → scenario: solo_work_session + good_for_calls, NOT category: cafe
- "first date ideas" → scenario: first_date, category: [restaurant, bar, rooftop_bar]

### Rule 2: Work Intent Detection
Queries containing any of: work, laptop, focus, concentrate, WiFi, outlet, charge → add work_suitability cluster to atmospheric requirements.

Work intent sub-classification:
- "deep work" / "focus" / "concentrate" → good_for_deep_work: true, noise_level: quiet-silent
- "team meeting" / "client meeting" → good_for_meetings: true, noise_level: quiet-moderate
- "calls" / "video call" → good_for_calls: true, noise_level: quiet

### Rule 3: Outdoor vs. Indoor Resolution
- "hike", "trail", "park", "nature", "outdoor" → outdoor cluster, trail_access_direct: true
- "cozy", "warm", "rainy day", "weather" → indoor preference, rain_day_retreat scenario tag

### Rule 4: Time Context Extraction
- "right now", "currently" → trigger dynamic layer lookup (real-time busyness)
- "morning", "evening", "late night" → best_time_of_day filter
- Day names → best_days filter

### Rule 5: Ambiguity Handling
If category cannot be resolved with >70% confidence:
- Return top 3 category candidates ranked by match score
- Present as "I found X matching spots — here are the best [category A], [category B], and [category C]"
- Never guess a single wrong category — the user would rather see multiple options

## Agent Routing Based on Intent

| Intent Type | Primary Agent | Supplementary |
|-------------|--------------|---------------|
| Local venue discovery | ScraperAgent (Nimble Maps) | ValidatorAgent |
| Outdoor/nature | ScraperAgent + GlobalIntelligenceAgent | — |
| Work/productivity | ScraperAgent | atmospheric scoring weighted |
| Event-adjacent | ScraperAgent + dynamic layer | GlobalIntelligenceAgent |
| Cross-city comparison | GlobalIntelligenceAgent | ScraperAgent per city |

## Scoring Weights by Query Type

Work queries: atmospheric match weight +15%, busyness inversion weight +10%
Outdoor queries: outdoor cluster weight +20%, trail_difficulty_match +10%
Social/date queries: ambiance weight +20%, scenario_tag_match +15%
Discovery queries (no clear intent): balanced weights, diversity penalty (prefer spread across neighborhoods)

## Neutrality and Accuracy Rules

1. Never recommend a venue that is_permanently_closed
2. Never present busyness data older than 30 minutes as current
3. Never recommend venues with < 10 total_ratings unless no alternatives exist (flag when used)
4. When confidence is low, state it explicitly in the synthesis: "Based on limited signals..."
5. Never fabricate atmospheric attributes — only cite signals that exist in the KB with signal_count > 0
""",
    },
    {
        "folder": "agent-routing-rules",
        "title": "2026-05-23 - Multi-Agent Pipeline Architecture",
        "text": """Source: Internal — agentic-engineering-hack/backend/agents/

# Multi-Agent Pipeline Architecture

## Pipeline Overview

The Right Spot runs three parallel agents per search query, coordinated by an Orchestrator. Each agent is responsible for a distinct layer of venue intelligence.

```
User Query (SSE stream opens)
    │
    ▼
Orchestrator.parse_intent()          ← Claude: extract structured VenueSearchIntent
    │
    ├─── ScraperAgent.scrape()        ← Nimble SERP: Google Maps + SERP review extraction
    ├─── ValidatorAgent.validate()    ← Cross-reference signals, flag low-confidence venues
    └─── GlobalIntelligenceAgent()   ← City benchmarks, neighborhood context, competitor signals
    │
    ▼
ClickHouse scoring                   ← Multi-factor venue ranking
    │
    ▼
Orchestrator.synthesize()            ← Claude: generate conversational recommendations
    │
    ▼
SSE stream to frontend               ← Real-time progressive results
    │
    ▼
PublisherAgent.publish()             ← Senso: publish intelligence as GEO-citable content
```

## ScraperAgent

Runs two Nimble API calls in sequence:
1. google_maps engine: Returns place_ids, coordinates, names, ratings, review counts
2. google engine: Returns review snippets, web mentions, cross-platform signals

Merges results by normalized venue name (Levenshtein distance < 0.2 = same venue).

Retry logic: up to 3 attempts with exponential backoff on rate limit (429) responses. rate_limited tag set in Datadog span if retry triggered.

## ValidatorAgent

Takes ScraperAgent output and applies three validation checks:
1. Identity validation: place_id exists in Google Places API (prevents hallucinated venues)
2. Freshness validation: last_verified timestamp < 7 days (or triggers refresh)
3. Signal confidence: venues with signal_count < 5 flagged as low-confidence

Returns validated venue list with confidence_score per venue.

## GlobalIntelligenceAgent

Provides city-level and neighborhood-level context:
- City benchmarks from ClickHouse AggregatingMergeTree (average ratings, price levels per city)
- Neighborhood characterization (based on aggregate atmospheric attributes of venues in that area)
- Competitive signal: "this venue scores in the top 15% for deep work in NYC"

## Orchestrator

Coordinates the three agents using asyncio.gather() for true parallel execution. Applies scoring algorithm in ClickHouse. Calls Claude for synthesis using cached system prompt (cache_control: ephemeral) to minimize latency and token cost on repeat queries.

## PublisherAgent

After search completes, optionally publishes venue intelligence to Senso:
- Generates a citeable summary of the search results
- Uploads to Senso KB as a spatial curation document
- Published citeables become AI-discoverable for future similar queries

## Datadog APM Spans

Every agent operation is traced:
- ai_span: parse_intent, synthesize (Claude calls)
- http_span: nimble_maps, nimble_serp (Nimble API calls)
- db_span: cache_check, score_venues, upsert_venues (ClickHouse operations)
- search_span: root span for the full query pipeline

Distributed trace ID flows through SSE stream to frontend for end-to-end observability.
""",
    },
    {
        "folder": "basemap-geometry",
        "title": "2026-05-23 - Competitive Landscape — The Right Spot vs Google Maps, Yelp, Foursquare",
        "text": """Source: Competitive research + Internal analysis

# Competitive Landscape — Venue Discovery in 2026

## The Market Shift

In April 2026, TechCrunch reported Google Maps is "about to get a big dose of AI" — generative AI capabilities that add enhanced visual and data analytics powers. Google Maps is evolving from navigation into a discovery platform. AI Overviews now appear in both traditional search and Maps, pulling from the same knowledge base.

This is the exact market The Right Spot operates in. The question isn't whether AI will transform venue discovery — it's whether the transformation will be controlled by the platforms that own the data, or enabled by platforms that understand intent better.

## Competitor Analysis

### Google Maps
Strengths: Comprehensive POI database, real-time Popular Times, AI Overviews in Maps, trusted by default
Weakness for our use case: Category search, not intent search. "Quiet cafe for deep work" returns the same results as "cafe." No atmospheric attribute layer. No conversational follow-ups.
2026 status: Adding AI but it surfaces from their existing data — no structured atmospheric signals.

### Yelp
Strengths: Deep review corpus, Elite recommendations, business photos, curated collections
Weakness: Review-centric, not intent-centric. Strong for restaurants, weak for work/outdoor use cases. No real-time busyness. No atmospheric attribute taxonomy.
2026 status: No significant AI integration in discovery layer.

### Foursquare Places API
Strengths: Global POI dataset, strong categorization system, structured place data, behavior signals
Weakness: API-first (no consumer product), no atmospheric attributes, no conversational search
2026 status: Strong for developers building location-aware apps; not a direct consumer competitor.

### Qloo (Cultural AI)
Strengths: Cross-domain taste intelligence (music → venues → travel), privacy-first API
Weakness: Recommendation engine, not discovery. Answers "you'd like X" not "I need Y right now."
2026 status: B2B API product, not consumer-facing discovery.

## Where The Right Spot Wins

| Dimension | Google Maps | Yelp | Foursquare | The Right Spot |
|-----------|-------------|------|------------|----------------|
| Intent parsing | Keyword match | Keyword match | Keyword match | Multi-agent semantic |
| Atmospheric signals | Partial (popular times) | Qualitative only | None | Structured taxonomy |
| Scenario tags | None | None | None | 30-tag ontology |
| Real-time data | Yes | No | No | Yes (Nimble SERP) |
| Conversational follow-up | Limited (AI Overview) | No | No | Yes (follow-up chips) |
| Traceability / explainability | No | No | No | Full Datadog APM |
| AI-citable content | No | No | No | Yes (Senso GEO) |

The Right Spot's structural advantage: it separates fact from opinion (POI Core vs. Atmospheric Attributes) and layers real-time dynamic signals on top — the same multi-tier architecture Google uses internally, available as a consumer product.
""",
    },
    {
        "folder": "reviews-sentiment",
        "title": "2026-05-23 - Review Signal Quality — Consensus Building and Trust Scoring",
        "text": """Source: Internal — atmospheric.schema.json + orchestrator.py

# Review Signal Quality — How We Build Trust Scores

## The Confidence Problem

User reviews are noisy. A single reviewer who "always brings their own lunch" will mark "no food menu" as a complaint. A reviewer who expects club music will rate a quiet cafe's "boring" ambiance. The Right Spot's scoring doesn't treat all signals equally — it builds consensus from signal diversity.

## Confidence Scoring Formula

For each atmospheric attribute:

```
confidence = (positive_signal_count / total_signal_count) * source_diversity_factor
```

Where:
- positive_signal_count = reviewers who mentioned the attribute positively
- total_signal_count = all reviewers who mentioned the attribute (positive or negative)
- source_diversity_factor = min(1.0, distinct_source_count / 3) — saturates at 3 independent sources

## Trust Thresholds

| signal_count | Confidence Level | Display Behavior |
|-------------|-----------------|-----------------|
| < 10 | Low | Flag as "based on limited signals" |
| 10–50 | Moderate | Show attribute without caveat |
| > 50 | High | Show with confidence indicator |
| source_diversity > 0.7 | Corroborated | Used in definitive claims |

## What Counts as an Independent Source

Three signal types are treated as independent:
1. Nimble SERP google_maps engine (Google review snippets)
2. Nimble SERP google engine (cross-platform web reviews)
3. Claude extraction from explicit review text vs. implicit sentiment

Two signals from the same source (same Google review batch) count as 1 independent source, not 2.

## Staleness Detection

Atmospheric attributes decay at different rates:
- WiFi quality: 90-day TTL (infrastructure changes slowly)
- Noise level: 30-day TTL (business model changes can shift this quickly)
- Crowding/busyness: 7-day TTL (seasonal + event-driven)
- Outdoor seating: 180-day TTL (rarely changes except for permits)

Signals past their TTL are still shown but flagged as "last verified [date]."

## Anti-Gaming Rules

1. Signals from reviewers with < 5 total reviews are weighted at 50%
2. Signals from reviewers who only ever gave 1-star or 5-star reviews are weighted at 70%
3. Review spikes (10+ reviews in 7 days) trigger a fraud flag and manual review queue
4. No single reviewer can shift an attribute consensus by more than 5 percentage points
""",
    },
]


def ingest(doc: dict) -> dict:
    payload = {
        "title": doc["title"],
        "text": doc["text"],
        "kb_folder_node_id": FOLDER_IDS[doc["folder"]],
    }
    result = subprocess.run(
        ["senso", "kb", "create-raw", "--data", json.dumps(payload), "--output", "json", "--quiet"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        data = json.loads(result.stdout.split("\n", 1)[1] if result.stdout.startswith("  ✓") else result.stdout)
        return {"ok": True, "id": data.get("id"), "title": doc["title"], "folder": doc["folder"]}
    return {"ok": False, "title": doc["title"], "folder": doc["folder"], "error": result.stderr}


if __name__ == "__main__":
    results = []
    for doc in DOCUMENTS:
        r = ingest(doc)
        status = "✓" if r["ok"] else "✗"
        print(f"  {status} {r['folder']:25s} {r['title'][:60]}")
        results.append(r)

    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    print(f"\n{ok} ingested, {fail} failed")
    if fail:
        for r in results:
            if not r["ok"]:
                print(f"  FAILED: {r['title']} — {r.get('error', '')}")
    sys.exit(0 if fail == 0 else 1)
