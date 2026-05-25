# 🗺️ The Right Spot

> **AI-powered venue intelligence that transforms Google Maps into a conversational discovery assistant.**

## 🔗 Live Demo

### **[https://agent-google-maps-nine.vercel.app](https://agent-google-maps-nine.vercel.app)**

Instead of returning a pile of pins, The Right Spot understands *intent* — and returns the right spot for your specific situation, time, and mood.

---

## ✨ What It Does

| You type | You get |
|---|---|
| `"quiet cafe for deep work in SoHo"` | Venues scored by WiFi quality, noise level, and time-of-day crowd data |
| `"hiking trails near me"` | Real trail recommendations anchored to your GPS position, not a city name |
| `"first date spot in a new city"` | Atmospheric matches: dimly lit, conversational noise level, impressive but not intimidating |
| `"best restaurant near me"` | GPS-biased search with a 15 km radius — falls back to map center if location permission is denied |
| `"public libraries near me"` | Non-restaurant venue types routed to the correct Google Places query |
| `"offices to scout near Midtown"` | Neighborhood clustering with transit scores and coworking density |
| *Type "montclair" in the address bar* | Autocomplete suggests as you type; one click pans the map and searches within 10 km |

### Map Features
- **Go To This Area & Search** — type any address, neighborhood, or city in the address bar; Google Places Autocomplete suggests as you type; hitting the button geocodes the input, pans the map, and runs a fresh 10 km radius search anchored to that location
- **Search This Area** — drag or zoom the map, hit the button and the search re-runs anchored to the visible viewport (radius derived from zoom level)
- **User location dot** — GPS position persisted in `localStorage` (30 min TTL) so the dot survives page refreshes; "near me" falls back to map center if location permission is denied
- **Intent chips** — parsed occasion, cuisine, noise preference, and price band shown as tappable refinement chips below the search bar
- **All Matches modal** — "View All ↗" opens a searchable, filterable grid of every result; deduplicates venues by name in case ClickHouse `ReplacingMergeTree` hasn't merged yet
- **Resizable panels** — left AI panel and right venue detail sidebar are both drag-resizable; left panel is collapsible

### 🗺️ Directions (In-Map Routing)
- **Get Directions button** — appears in the venue detail sidebar header; renders the route directly on the map using Google Maps `DirectionsService` + `DirectionsRenderer` (no redirect to a separate app)
- **Five travel modes** — Transit 🚇, Drive 🚗, Walk 🚶, Bike 🚲, Fly ✈️ — modern segmented control with color-coded gradient buttons and glow effects
- **Multiple route alternatives** — Drive, Transit, Walk, and Bike modes return up to 3 alternative routes; tap any option in the sidebar to switch the rendered polyline and update the duration/distance display
- **Auto-recalculate on mode switch** — switching travel modes immediately re-runs directions for the currently selected venue with no extra tap required
- **Smart destination fallback** — uses Google Place ID when available, falls back to lat/lng coordinates, then to venue name + address as a text query — so directions always work even for venues without stored coordinates
- **Clear Route** — button toggles to "Clear Route" once a route is active; clears the polyline from the map
- **External directions chip** — each venue card in the left panel has a "🗺️ Directions" chip that opens Google Maps with the destination pre-filled (Place ID → coordinates → name/address text query)
- **GPS error handling** — if location permission is denied, an inline error prompts the user to enable it instead of silently failing

### ✈️ Flight Search
- **Auto-Fly mode** — when you select a venue more than 500 km from your current location, the travel mode automatically switches to Fly; switching back to a ground mode resets it to Transit
- **Serpapi Google Flights** — the `✈️ Fly` mode calls the `/api/flights` backend endpoint, which queries the Serpapi Google Flights API for real one-way fare data
- **Up to 3 flight options** — results are sorted cheapest-first; each card shows price, total flight duration, number of stops, airline, flight number, and departure/arrival airport names
- **Pick your flight** — tap any flight option in the sidebar to make it the active selection, exactly like picking a driving route alternative
- **Smart hub routing** — the backend uses a 130-airport Haversine database and automatically redirects secondary airports to the correct international hub (e.g. LGA → JFK, LCY → LHR, MDW → ORD, DCA → IAD) so searches from city-center coordinates always resolve to a hub with long-haul service
- **Requires `SERPAPI_API_KEY`** — get a free key at [serpapi.com](https://serpapi.com); flight search is gracefully disabled if the key is absent

### 🚇 Nearby Transit
- **Nearby Transit button** — floating button on the map fetches airports, subway stations, bus stops, train stations, and ferry terminals within 1.5 km of the current map center using the Google Places Nearby Search API
- **Color-coded markers** — each transit type has a distinct color and emoji pin (🚇 subway amber, 🚌 bus green, ✈️ airport purple, 🚆 train blue, ⛴️ ferry cyan)
- **Transit info window** — clicking a transit marker shows its name, address, and a directions link
- **Toggle on/off** — markers are created once and shown/hidden with `marker.map` assignment (no DOM teardown = no flashing)
- **Loading skeleton** — animated skeleton rows appear while the API call is in-flight so the UI never looks frozen
- **Transit stop filtering** — backend scraper agent blocks transit stations (`subway_station`, `train_station`, `bus_stop`, etc.) from being ingested as venue results

### 🏘️ Suburb & Rural Search
- **GPS reverse geocoding** — when coordinates are present the backend reverse-geocodes them to derive the exact town, county, and state, overriding the LLM's city default so searches for North Caldwell, Maplewood, or any suburb return local results instead of the nearest major city
- **Hyper-local query building** — up to 8 parallel Google Places queries are generated using the GPS-derived area string (e.g. "North Caldwell New Jersey", "Essex County New Jersey") rather than a broad city name
- **Google rating in scoring** — `google_rating` is stored in ClickHouse and adds 0–15 pts to each venue's match score, spreading results across a realistic range even when Claude has no review snippet to extract signals from
- **In-memory fallback scoring** — if ClickHouse has no cached venues for a new location, the orchestrator scores the freshly scraped results in memory (including the rating bonus) so the first search in any area always returns results

---

## 🏗️ Architecture

```
User Query + GPS coords (optional)
        │
        ▼
┌───────────────────────────────────┐
│         Orchestrator              │  ← Claude: parse intent → VenueIntent
│  (FastAPI SSE streaming)          │
└──────────┬────────────────────────┘
           │  asyncio.gather() — parallel
    ┌──────┴──────┬──────────────────┐
    ▼             ▼                  ▼
ScraperAgent  ValidatorAgent  GlobalIntelligenceAgent
 Phase 1:     (confidence)    (city benchmarks)
  Google Places API (text search + locationBias.circle)
  Nimble google_maps (local pack, place IDs) ─ best-effort 10s
  Nimble google_search (Yelp/TripAdvisor snippets) ─ best-effort
 Phase 2:
  Claude signal extraction (noise, capacity, occasion score…)
    └──────┬──────┴──────────────────┘
           │
           ▼
┌───────────────────────────────────┐
│       ClickHouse Scoring          │  ← Multi-factor ranking
│  ReplacingMergeTree venue_signals │    base 25 + capacity + noise +
│                                   │    occasion fit + price band +
│                                   │    Google rating bonus (0-15 pts)
└──────────┬────────────────────────┘
           │
           ▼
┌───────────────────────────────────┐
│     Claude Synthesis              │  ← Why-card, scenario, sensitivity bars
│  (prompt caching: ephemeral)      │    for top 10 venues
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
![Serpapi](https://img.shields.io/badge/Serpapi-Google_Flights-4285F4?style=flat)
![Datadog](https://img.shields.io/badge/Datadog-APM-632CA6?style=flat&logo=datadog&logoColor=white)

---

## 📁 Project Structure

```
agentic-engineering-hack/
├── backend/
│   ├── agents/
│   │   ├── orchestrator.py        # Intent parsing + synthesis (Claude)
│   │   ├── scraper_agent.py       # Phase 1: Google Places + Nimble; Phase 2: Claude signals
│   │   ├── validator_agent.py     # Result confidence scoring
│   │   ├── global_agent.py        # City-level benchmark intelligence
│   │   └── publisher_agent.py     # Senso GEO publishing
│   ├── api/
│   │   └── server.py              # FastAPI + SSE streaming endpoints
│   ├── db/
│   │   └── clickhouse.py          # MergeTree schema + multi-factor scoring query
│   ├── integrations/
│   │   ├── google_maps_client.py      # Places API (real-time, TOS compliant, locationBias)
│   │   ├── nimble_client.py           # Nimble google_maps + google_search engines
│   │   ├── serpapi_flights_client.py  # Serpapi Google Flights (✈️ Fly mode, 130-airport DB)
│   │   └── senso_client.py            # Senso knowledge base client
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

## ☁️ Vercel Deployment

The repo is structured for Vercel's `experimentalServices` monorepo support:

```json
// vercel.json
{
  "experimentalServices": {
    "frontend": { "root": "frontend", "framework": "nextjs", "routePrefix": "/" },
    "backend":  { "root": "backend", "entrypoint": "api.server:app",
                  "routePrefix": "/_/backend", "maxDuration": 60 }
  }
}
```

Set all `.env` variables as **encrypted environment variables** in the Vercel dashboard (Settings → Environment Variables). Never commit `.env` to version control.

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
# AI / data
ANTHROPIC_API_KEY=sk-ant-...       # console.anthropic.com
NIMBLE_API_KEY=...                  # nimbleway.com → Dashboard (optional — falls back to Google Places only)
SENSO_API_KEY=tgr_...              # app.senso.ai → Settings

# Flight search (✈️ Fly mode)
SERPAPI_API_KEY=...                 # serpapi.com → Dashboard (optional — Fly mode disabled if absent)

# Google Maps
GOOGLE_MAPS_API_KEY=AIza...        # console.cloud.google.com
GOOGLE_MAPS_MAP_ID=...             # Google Maps Platform → Map Management
GOOGLE_MAPS_REFERER=http://localhost:3000   # server-side referer header for key restrictions
NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=AIza...     # browser-side (same key, browser-restricted)
NEXT_PUBLIC_GOOGLE_MAPS_MAP_ID=...

# ClickHouse Cloud
CLICKHOUSE_HOST=<your>.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=...
CLICKHOUSE_SECURE=true

# Datadog APM (optional for local dev)
DD_API_KEY=...
DD_ENV=local
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
| **Google Maps** | Map rendering + real-time place details + directions | [console.cloud.google.com](https://console.cloud.google.com) |
| **Serpapi** | Google Flights data for the ✈️ Fly travel mode | [serpapi.com](https://serpapi.com) |
| **Datadog** | APM tracing (optional for local dev) | [app.datadoghq.com](https://app.datadoghq.com) |

---

## 🗺️ Google Maps Setup

Enable these APIs in Google Cloud Console:

1. **Maps JavaScript API** — map rendering + AdvancedMarkerElement
2. **Places API (New)** — text search with `locationBias`, real-time place details, and nearby transit search
3. **Geocoding API** — reverse geocoding for Search This Area label + user location city detection
4. **Directions API** — in-map route rendering via `DirectionsService` (Transit, Drive, Walk, Bike modes)

Create a **Map ID** at Google Maps Platform → Map Management (Vector type with tilt + rotation for 3D building views).

**Key restrictions** (HTTP referrers):
```
localhost:3000/*
https://agent-google-maps-nine.vercel.app/*
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
