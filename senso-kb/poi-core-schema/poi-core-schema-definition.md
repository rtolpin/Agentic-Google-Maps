# POI Core Schema — The Right Spot

## What This Is

The POI Core Schema is The Right Spot's foundational taxonomy layer for every Point of Interest (venue) in the system. It defines the canonical identity of a place: what it is, where it is, when it's open, and how it's classified. This layer is the ground truth that every other data layer references — atmospheric attributes, review sentiment, and dynamic signals all link back to a `place_id` defined here.

**Schema file:** `backend/schemas/poi-core.schema.json`  
**Senso folder:** `/poi-core-schema`  
**Version:** 1.0.0

---

## Why It Exists

Separating core identity (what a place *is*) from qualitative signals (what a place *feels like*) prevents a critical LLM reasoning error: blending facts with opinions. Without this separation, an AI answering "Is The Reading Room Cafe open on Sundays?" might contaminate its answer with sentiment data ("users love it on weekdays"). The POI Core Schema guarantees clean, authoritative factual answers.

---

## Primary Type Taxonomy

Every venue is assigned exactly one `primary_type` from Google Places API. This is the single authoritative classification used for identity resolution — it prevents the AI from confusing venues with similar names or multiple use cases.

### Workspace & Productivity
| Type | Description |
|---|---|
| `cafe` | General coffee shop with seating |
| `coffee_shop` | Specialty coffee focus |
| `coworking_space` | Dedicated shared workspace |
| `office` | Corporate office building |
| `business_center` | Hotel or building business facilities |
| `library` | Public or university library |

### Nature & Outdoor
| Type | Description |
|---|---|
| `park` | Urban or suburban public park |
| `national_park` | Protected wilderness area |
| `hiking_area` | Designated hiking terrain |
| `campground` | Overnight camping facilities |
| `nature_reserve` | Protected natural habitat |
| `beach` | Ocean, lake, or river beach |

### Food & Social
| Type | Description |
|---|---|
| `restaurant` | Full-service dining |
| `bar` | Alcohol-primary venue |
| `bakery` | Baked goods and light fare |
| `food_court` | Multi-vendor indoor food space |
| `rooftop_bar` | Elevated outdoor bar |

### Culture & Experience
| Type | Description |
|---|---|
| `museum` | Curated exhibitions |
| `art_gallery` | Visual art display space |
| `cultural_center` | Community arts and culture hub |
| `book_store` | Retail books, often with seating |
| `tourist_attraction` | Destination landmark |
| `landmark` | Historic or architectural landmark |

---

## Required Fields

Every POI record must include:

- **`place_id`** — Google's globally unique identifier. The neutral key that links all data layers together. Safe to store permanently.
- **`name`** — Display name as returned by Google Places API.
- **`coordinates`** — `lat`/`lng` decimal degrees (WGS84). Used for proximity queries, geohash indexing, and map rendering.
- **`primary_type`** — Single classification from the taxonomy above. Required for identity resolution.
- **`address.formatted`** — Full human-readable address for display.
- **`address.city`** — City-level filter for geographic search.
- **`address.country`** — ISO country for internationalization.

---

## Address Structure

The address object decomposes a venue's location for multi-resolution queries:

```
formatted   → full display string
street      → street name (for "on X street" queries)
neighborhood → sub-city area (e.g., "SoHo", "Mission District")
city        → city-level geographic filter
state       → state/province
country     → ISO 3166-1 alpha-2 (e.g., "US", "GB")
postal_code → zip/postcode
```

The `neighborhood` field is especially important for The Right Spot — most qualitative venue searches are neighborhood-scoped ("quiet cafe in the West Village", "hiking near Capitol Hill").

---

## Hours Format

Operating hours use two representations:

**Structured periods** (for programmatic queries — "is it open right now?"):
```json
{ "day": 1, "open_time": "0800", "close_time": "2200" }
```
Day 0 = Sunday, day 6 = Saturday. Times in 24-hour HHMM format.

**Weekday text** (for display and LLM citation):
```
["Monday: 8:00 AM – 10:00 PM", "Tuesday: 8:00 AM – 10:00 PM", ...]
```

---

## Ratings

| Field | Type | Scale | Notes |
|---|---|---|---|
| `google_rating` | float | 0–5 | 1 decimal place |
| `total_ratings` | integer | — | Trust signal — prefer venues with 50+ ratings |
| `price_level` | integer | 0–4 | 0=Free, 1=$, 2=$$, 3=$$$, 4=$$$$ |

---

## Spatial Enrichment

The `spatial` object extends the core coordinates with derived geospatial intelligence:

- **`geohash`** — Precision-7 geohash (~150m accuracy) for fast bounding-box queries in ClickHouse without trigonometric calculations.
- **`neighborhood_polygon_id`** — Links to the `/basemap-geometry` layer for administrative boundary lookups.
- **`transit_score`** — 0–100 walkability/transit score. A venue with score 85+ can be described as "transit-accessible" in AI responses.
- **`walk_time_to_transit_minutes`** — Direct answer to "how far is the nearest subway?"

---

## Google TOS Compliance

**Critical:** `contact` fields (`phone`, `website`, `google_maps_uri`) are **display-only**. They must never be persisted to ClickHouse or any long-term data store. The only Google-sourced identifier safe for permanent storage is `place_id`.

The `place_id` field is explicitly designed as a neutral identifier. Google's terms permit its storage as a reference key. All other Google Place Details must be fetched fresh at display time.

---

## Data Sources

| Source | What it provides | When to use |
|---|---|---|
| `nimble_maps` | place_id, coordinates, name, primary_type | Initial discovery pass |
| `nimble_serp` | review snippets, supplementary metadata | Signal enrichment |
| `google_places_api` | hours, contact, ratings, secondary_types | Authoritative display data (fetch-on-demand) |
| `manual` | Editorial overrides, corrections | Human curation |

---

## Example Record

```json
{
  "place_id": "ChIJrTLr-GyuEmsRBfy61i59si0",
  "name": "The Reading Room Cafe",
  "coordinates": { "lat": 40.7128, "lng": -74.0060 },
  "primary_type": "cafe",
  "secondary_types": ["book_store"],
  "address": {
    "formatted": "142 Prince St, New York, NY 10012, USA",
    "neighborhood": "SoHo",
    "city": "New York",
    "state": "NY",
    "country": "US",
    "postal_code": "10012"
  },
  "ratings": {
    "google_rating": 4.6,
    "total_ratings": 892,
    "price_level": 2
  },
  "metadata": {
    "data_source": "nimble_maps",
    "ingested_at": "2025-05-23T10:00:00Z",
    "schema_version": "1.0.0"
  }
}
```

---

## How AI Models Use This Layer

When an AI is asked:
- *"Find a bookstore cafe in SoHo that's open past 9 PM"* → filters `primary_type: cafe`, `secondary_types: book_store`, `address.neighborhood: SoHo`, `hours.periods.close_time >= 2100`
- *"What type of place is The Reading Room Cafe?"* → returns `primary_type` + `secondary_types` for unambiguous identity
- *"How expensive is it?"* → maps `price_level: 2` to "moderately priced ($$)"
- *"Is it well-reviewed?"* → combines `google_rating: 4.6` with `total_ratings: 892` for confidence signal

This layer never answers "is it quiet" or "good for work" — those belong in `/atmospheric-attributes`.
