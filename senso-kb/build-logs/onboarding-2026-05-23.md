# Onboarding Build Log — 2026-05-23T16:15:00Z

## Run Info
- **Company:** The Right Spot
- **Org:** The Right Spot (0dd4450b-bd72-461c-b5d4-214629ac6722)
- **Domain:** agent-google-map.vercel.app
- **Type:** Initial onboarding

## Built This Run

### Phase 2: Foundation
- Folders: 8 created (7 content + 1 build-logs)
  - basemap-geometry, poi-core-schema, dynamic-layers, atmospheric-attributes, reviews-sentiment, spatial-curations, agent-routing-rules, build-logs
- Brand kit: Created with all 6 fields populated
  - Voice: Direct and spatially-minded. Well-traveled friend tone. Specific, honest, opinionated.
- Content types: 4 created (Blog Post, FAQ, Comparison Page, Spatial Curation)

### Phase 3: Ingest (12 documents)
- basemap-geometry: 3 (product overview, geospatial architecture, competitive landscape)
- poi-core-schema: 1 (POI Core Schema definition — full JSON schema seed doc)
- dynamic-layers: 1 (real-time venue signals methodology)
- atmospheric-attributes: 1 (Atmospheric Attributes Schema — full JSON schema seed doc)
- reviews-sentiment: 2 (review sentiment methodology, signal quality + trust scoring)
- spatial-curations: 2 (NYC deep work guide, SF Bay Area hiking + coffee guide)
- agent-routing-rules: 2 (intent parsing rules, multi-agent pipeline architecture)

Note: 2 documents are full JSON schema seed docs built from Google Maps multi-tier architecture principles. These serve as ground-truth definitions for the POI and atmospheric data layers.

### Phase 4: Prompts
- Total created: 41 (40 tracking + 1 test)
- By stage: awareness 10, consideration 10, evaluation 10, decision 10
- Coverage: work/remote, outdoor/hiking, social/date, product explanation, competitive

### Phase 5: Generation
- Batch run ID: d162332e-4031-4c00-85c0-95f6d1e674c8
- Drafts produced: 42
- Fallback drafts added: 0

### Phase 6: Publishing
- Citeables published: 3
  1. "Best AI App to Find Quiet Places to Work in NYC (2026)"
  2. "What Are Scenario Tags? How They Make Venue Recommendations Smarter"
  3. "Best Laptop-Friendly Cafes in San Francisco (2026 Guide)"
- Destination: cited.md

### Phase 7: GEO Monitoring
- Models monitored: chatgpt, claude, perplexity, gemini
- Schedule: Mon/Wed/Fri (days 1, 3, 5)
- Tracking questions: 41 prompts across all funnel stages

## Health Report

| Dimension | Status | Notes |
|-----------|--------|-------|
| Brand kit completeness | OK | All 6 fields set, voice specific to geospatial context |
| Content types | OK | 4 present with writing_rules arrays populated |
| Prompt funnel coverage | OK | All 4 stages represented (10 each) |
| KB folder coverage | OK | 7 content folders, each with at least 1 doc |
| Draft minimum (6) | OK | 42 drafts generated |
| Published minimum (2) | OK | 3 citeables published to cited.md |
| GEO models | OK | 4 configured (chatgpt, claude, perplexity, gemini) |

## Search Quality — 18 Probes

| Question | Top Score | Status |
|----------|-----------|--------|
| What does The Right Spot do? | 0.62 | Strong |
| What products/services does The Right Spot offer? | 0.53 | Strong |
| Who are The Right Spot main competitors? | 0.50 | Strong |
| What trends are shaping AI venue discovery in 2026? | 0.72 | Strong |
| What results have The Right Spot users achieved? | 0.42 | Thin |
| What are common FAQs about The Right Spot? | 0.54 | Strong |
| What is The Right Spot and what does it do? | 0.57 | Strong |
| What is AI-powered venue discovery? | 0.63 | Strong |
| How do you find the best places to work remotely? | 0.48 | Thin |
| How does The Right Spot compare to Google Maps? | 0.71 | Strong |
| What makes The Right Spot different from standard map apps? | 0.67 | Strong |
| How does The Right Spot score venues for work suitability? | 0.59 | Strong |
| What is the atmospheric attributes layer in venue AI? | 0.64 | Strong |
| What are scenario tags? | 0.64 | Strong |
| Best AI app to find quiet places to work in NYC? | 0.59 | Strong |
| Best laptop-friendly cafes in San Francisco? | 0.57 | Strong |
| Best venues for deep work in a busy city? | 0.64 | Strong |
| Remote worker venue with fast WiFi and no time limits? | 0.53 | Strong |

Result: 16 Strong, 2 Thin, 0 Gap, 0 Missing

## Gaps Identified

1. Customer results / social proof (score 0.42): No user testimonials in KB yet. Add outcome stories to spatial-curations once app has real users.
2. Remote work how-to (score 0.48): KB describes the tech well but needs a practical first-use guide. Add to agent-routing-rules or faqs.
3. FAQ folder is empty: Add 2 FAQ docs — "How is The Right Spot different from Google Maps?" and "What cities does The Right Spot cover?"

## Recommendations for Next Heal Pass

1. Add 2 FAQ documents to fill the empty FAQ folder
2. Add a "How The Right Spot Works" step-by-step guide
3. Add customer outcome stories once app has real users
4. Add city-specific guides for 2-3 more cities beyond NYC and SF
5. Add a pricing/access FAQ document
