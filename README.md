# 🗺️ The Right Spot

> **AI-powered venue intelligence that transforms Google Maps into a conversational discovery assistant.**

## 🔗 Live Demo

### **[→ agentic-engineering-hack.vercel.app](https://agentic-engineering-hack.vercel.app)**

Instead of returning a pile of pins, The Right Spot understands *intent* — and returns the right spot for your specific situation, time, and mood.

---

## ✨ What It Does

| You type | You get |
|---|---|
| `"quiet cafe for deep work in SoHo"` | Venues scored by WiFi quality, noise level, outlet availability, and time-of-day crowd data |
| `"post-hike coffee with a view near Twin Peaks"` | Trail-adjacent venues with scenic ratings, dog policy, and outdoor seating |
| `"first date spot in a new city"` | Atmospheric matches: dimly lit, conversational noise level, impressive but not intimidating |
| `"offices to scout near Midtown"` | Neighborhood clustering with transit scores and coworking density |

---

## 🏗️ Architecture

```
User Query (natural language)
        │
        ▼
┌───────────────────────────────────┐
│         Orchestrator              │  ← Claude: parse intent → VenueSearchIntent
│  (FastAPI SSE streaming)          │
└──────────┬────────────────────────┘
           │  asyncio.gather() — parallel
    ┌──────┴──────┬──────────────────┐
    ▼             ▼                  ▼
ScraperAgent  ValidatorAgent  GlobalIntelligenceAgent
(Nimble SERP) (confidence)    (city benchmarks)
    └──────┬──────┴──────────────────┘
           │
           ▼
┌───────────────────────────────────┐
│       ClickHouse Scoring          │  ← Multi-factor venue ranking
│  ReplacingMergeTree venue_signals │
└──────────┬────────────────────────┘
           │
           ▼
┌───────────────────────────────────┐
│     Claude Synthesis              │  ← Brand-voice recommendations
│  (prompt caching: ephemeral)      │
└──────────┬────────────────────────┘
           │
    ┌──────┴──────────────────┐
    ▼                         ▼
SSE → Frontend            PublisherAgent
(Google Maps UI)          (Senso GEO)
```

---

## 🧠 Geospatial Intelligence Layers

The Right Spot mirrors Google Maps' own multi-tier data architecture:

| Layer | Folder | What it answers |
|---|---|---|
| 🗺️ Base Map | `/basemap-geometry` | "within 5 min walk of X" |
| 📍 POI Identity | `/poi-core-schema` | "what type of place is this?" |
| ⚡ Real-Time | `/dynamic-layers` | "is it busy right now?" |
| 🌡️ Atmosphere | `/atmospheric-attributes` | "is it good for deep work?" |
| ⭐ Reviews | `/reviews-sentiment` | "what do people consistently say?" |
| 🏙️ Curations | `/spatial-curations` | "best spots for X in NYC" |
| 🤖 Routing | `/agent-routing-rules` | "how does the AI decide?" |

---

## 🛠️ Tech Stack

### Backend
![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=flat&logo=fastapi&logoColor=white)
![Anthropic](https://img.shields.io/badge/Claude-Sonnet_4.6-CC785C?style=flat)
![ClickHouse](https://img.shields.io/badge/ClickHouse-24.3-FFCC01?style=flat&logo=clickhouse&logoColor=black)
![Redis](https://img.shields.io/badge/Redis-7.2-DC382D?style=flat&logo=redis&logoColor=white)

### Frontend
![Next.js](https://img.shields.io/badge/Next.js-14-000000?style=flat&logo=nextdotjs&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.4-3178C6?style=flat&logo=typescript&logoColor=white)
![Google Maps](https://img.shields.io/badge/Google_Maps-JS_API_v3-4285F4?style=flat&logo=googlemaps&logoColor=white)

### Sponsor Integrations
![Anthropic](https://img.shields.io/badge/Anthropic-Claude_AI-CC785C?style=flat)
![Nimble](https://img.shields.io/badge/Nimble-SERP_API-00C49F?style=flat)
![Senso](https://img.shields.io/badge/Senso.ai-GEO-6366F1?style=flat)
![Datadog](https://img.shields.io/badge/Datadog-APM-632CA6?style=flat&logo=datadog&logoColor=white)

---

## 📁 Project Structure

```
agentic-engineering-hack/
├── backend/
│   ├── agents/
│   │   ├── orchestrator.py        # Intent parsing + synthesis (Claude)
│   │   ├── scraper_agent.py       # Nimble SERP: Maps + review extraction
│   │   └── publisher_agent.py     # Senso GEO publishing
│   ├── api/
│   │   └── server.py              # FastAPI + SSE streaming endpoints
│   ├── db/
│   │   └── clickhouse.py          # MergeTree schema + venue scoring queries
│   ├── integrations/
│   │   ├── google_maps_client.py  # Places API (real-time, TOS compliant)
│   │   └── senso_client.py        # Senso knowledge base client
│   ├── models/
│   │   └── models.py              # All Pydantic v2 domain types
│   ├── schemas/
│   │   ├── poi-core.schema.json   # POI taxonomy (44 primary types)
│   │   └── atmospheric.schema.json # 30 scenario tags, 6 attribute clusters
│   ├── tests/                     # pytest + SpanRecorder mock tracer
│   ├── tracing.py                 # Datadog APM spans (ai/db/http/search)
│   └── requirements.txt
├── frontend/
│   ├── app/
│   │   ├── layout.tsx
│   │   └── page.tsx               # Full-screen map entry point
│   ├── components/
│   │   └── VenueMap.tsx           # AdvancedMarkerElement + conversational UI
│   ├── hooks/
│   │   └── useVenueSearch.ts      # SSE streaming hook
│   └── lib/
│       └── tracing.ts             # Browser + server Datadog tracing
├── senso-kb/
│   ├── poi-core-schema/           # Seed doc: POI taxonomy definition
│   ├── atmospheric-attributes/    # Seed doc: qualitative signals definition
│   ├── spatial-curations/         # NYC deep work guide, SF hiking guide
│   ├── agent-routing-rules/       # Intent parsing + pipeline docs
│   └── build-logs/                # Onboarding heal report
└── infra/
    ├── docker-compose.yml
    └── clickhouse-config.xml
```

---

## 🚀 Getting Started

### Prerequisites

- Docker Desktop
- Node.js 20+
- Python 3.11+

### 1. Clone and configure

```bash
git clone https://github.com/your-username/agentic-engineering-hack.git
cd agentic-engineering-hack
cp .env.example .env
```

Fill in your `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...       # console.anthropic.com
NIMBLE_API_KEY=...                  # nimbleway.com → Dashboard
SENSO_API_KEY=tgr_...              # app.senso.ai → Settings
GOOGLE_MAPS_API_KEY=AIza...        # console.cloud.google.com
GOOGLE_MAPS_MAP_ID=...             # Google Maps Platform → Map Management
```

### 2. Start all services

```bash
cd infra
docker compose up --build
```

This starts:
- ClickHouse on `localhost:8123`
- Redis on `localhost:6379`
- FastAPI backend on `localhost:8000`
- Next.js frontend on `localhost:3000`

### 3. Open the app

```
http://localhost:3000
```

Type anything — `"quiet cafe for deep work"`, `"hiking near downtown"`, `"first date spot"` — and watch the AI layer unfold on the map.

---

## 🔑 Required API Keys

| Service | Purpose | Get it at |
|---|---|---|
| **Anthropic Claude** | Intent parsing, signal extraction, synthesis | [console.anthropic.com](https://console.anthropic.com) |
| **Nimble SERP** | Google Maps data extraction + review snippets | [nimbleway.com](https://nimbleway.com) |
| **Senso.ai** | GEO publishing + AI citation tracking | [app.senso.ai](https://app.senso.ai) |
| **Google Maps** | Map rendering + real-time place details | [console.cloud.google.com](https://console.cloud.google.com) |
| **Datadog** | APM tracing (optional for local dev) | [app.datadoghq.com](https://app.datadoghq.com) |

---

## 🗺️ Google Maps Setup

Enable these 3 APIs in Google Cloud Console:

1. **Maps JavaScript API** — map rendering
2. **Places API (New)** — real-time place details
3. **Geocoding API** — city name → coordinates

Create a **Map ID** at Google Maps Platform → Map Management (Vector type with tilt + rotation for 3D building views).

**Key restrictions** (HTTP referrers):
```
localhost:3000/*
https://agentic-engineering-hack.vercel.app/*
```

---

## 🧪 Running Tests

```bash
# Backend tests
cd backend
pip install -r requirements.txt
pytest tests/ -v

# Frontend tests
cd frontend
npm install
npm test
```

The backend uses a `SpanRecorder` mock tracer — no live Datadog agent needed for tests.

---

## 📊 Observability

Every operation is traced end-to-end with Datadog APM:

| Span type | Operations traced |
|---|---|
| `ai_span` | Claude intent parsing, signal extraction, synthesis |
| `http_span` | Nimble SERP calls, Google Maps API calls |
| `db_span` | ClickHouse venue scoring, cache checks, upserts |
| `search_span` | Root span for the full query pipeline |

---

## 🌐 Senso GEO Integration

The Right Spot publishes venue intelligence to [Senso.ai](https://senso.ai) so AI models (ChatGPT, Claude, Perplexity, Gemini) can cite it when users ask venue discovery questions.

**Knowledge base:** 13 documents across 8 geospatial folders  
**Tracking prompts:** 41 questions across awareness → decision funnel  
**Published citeables:** Live at [cited.md](https://cited.md)  
**GEO monitoring:** Mon/Wed/Fri across 4 AI models  

---

## 🏆 Hackathon

Built for the **Anthropic + Senso.ai Hackathon**.

**Sponsor integrations:**
- 🤖 [Anthropic Claude](https://anthropic.com) — multi-agent AI pipeline
- 🔍 [Nimble SERP](https://nimbleway.com) — real-time Google Maps data extraction
- 📡 [Senso.ai](https://senso.ai) — GEO publishing and AI citation tracking
- 🗺️ [Google Maps Platform](https://mapsplatform.google.com) — interactive map display
- 📈 [Datadog](https://datadoghq.com) — distributed APM tracing

---

## 📄 License

MIT
