# Atmospheric Attributes Schema — The Right Spot

## What This Is

The Atmospheric Attributes Schema captures the qualitative, experiential, and sensory characteristics of a venue — the things people actually say when they describe a place. It is deliberately separate from the POI Core Schema, which only captures objective facts. This separation means an AI can check whether a cafe is open (POI Core) without that factual lookup being contaminated by opinions about whether the vibe is right for a first date (Atmospheric).

**Schema file:** `backend/schemas/atmospheric.schema.json`  
**Senso folder:** `/atmospheric-attributes`  
**Version:** 1.0.0

---

## Why It Exists

Standard map data answers *what* and *where*. Atmospheric attributes answer *how it feels* and *what you'd use it for*. These are the signals that power qualitative semantic search:

- "quiet coffee shop for deep work" → `work_suitability.good_for_deep_work: true` + `ambiance.noise_level: quiet`
- "dimly lit bar for a first date" → `ambiance.lighting: dim` + `scenario_tags: first_date`
- "dog-friendly outdoor patio in the afternoon" → `accessibility.dog_friendly: outdoor_only` + `outdoor.has_outdoor_seating: true` + `time_sensitivity.best_time_of_day: afternoon`

No amount of Google Places category data answers these questions. Atmospheric attributes are the layer that makes The Right Spot's AI actually useful.

---

## Attribute Clusters

### 1. Work Suitability

Answers productivity and professional use queries. The highest-value cluster for The Right Spot's target user (remote workers, freelancers, nomads).

| Attribute | Type | What it means |
|---|---|---|
| `good_for_deep_work` | boolean | Low distraction, sustained concentration possible |
| `good_for_calls` | boolean | Acoustics allow voice/video without disruption |
| `good_for_meetings` | boolean | Suitable for 2–4 person professional conversations |
| `wifi_quality` | enum | none → poor → moderate → fast → excellent |
| `wifi_password_required` | boolean | Friction signal — relevant for quick visits |
| `power_outlets` | enum | none → scarce → moderate → abundant |
| `laptop_friendly` | boolean | Table height/space accommodates laptop use |
| `quiet_zones_available` | boolean | Dedicated quiet section exists |
| `time_limit_enforced` | boolean | Venue has a 2-hour or similar rule |
| `minimum_spend_required` | boolean | Must purchase to occupy a seat |

**Example query answered:** *"I need 4 hours of uninterrupted focus time, need power, and can't be kicked out at 2 PM"*
→ `good_for_deep_work: true`, `power_outlets: abundant`, `time_limit_enforced: false`

---

### 2. Ambiance

Sensory and aesthetic qualities. Powers queries about mood, environment, and how a space feels.

| Attribute | Values | Notes |
|---|---|---|
| `noise_level` | silent / quiet / moderate / lively / loud | Baseline during normal hours |
| `noise_variance` | consistent / time_variable / day_variable | Does it get loud at lunch? |
| `lighting` | dim / soft / moderate / bright / natural_dominant | Critical for evening and date queries |
| `seating_comfort` | poor / adequate / comfortable / very_comfortable | Matters for 3+ hour stays |
| `seating_types` | array | bar_stools, lounge_chairs, booth, standard_table, window_seats, sofa, communal_table, floor_seating |
| `crowding_typical` | rarely / moderately / often / always crowded | Baseline occupancy |
| `interior_style` | array (max 3) | cozy, industrial, minimalist, trendy, classic, bohemian, upscale, casual, academic, creative, rustic, modern, vintage, zen |
| `music_played` | none / background_instrumental / background_vocal / prominent / live_music | — |
| `scent_profile` | neutral / coffee / food / floral / woody / sea_air / fresh_air | Relevant for sensory-sensitive users |

**Example query answered:** *"Cozy, dimly lit place with soft background music for a date night"*
→ `lighting: dim`, `interior_style: [cozy]`, `music_played: background_instrumental`, `noise_level: quiet`

---

### 3. Accessibility

Physical and logistical access attributes. Critical for inclusive search.

| Attribute | Type | Notes |
|---|---|---|
| `wheelchair_accessible_entrance` | boolean | — |
| `wheelchair_accessible_seating` | boolean | — |
| `accessible_restroom` | boolean | — |
| `parking_available` | enum | none / street / paid_lot / free_lot / valet |
| `transit_proximity_minutes` | number | Walk time to nearest stop |
| `bike_parking` | boolean | — |
| `bike_share_nearby` | boolean | Citibike, BIKETOWN, etc. |
| `dog_friendly` | enum | no / outdoor_only / indoor_allowed |
| `kid_friendly` | boolean | Family appropriate |
| `stroller_accessible` | boolean | Wide aisles, ramp access |
| `gender_neutral_restroom` | boolean | — |

**Example query answered:** *"Dog-friendly cafe with outdoor seating and bike parking"*
→ `dog_friendly: outdoor_only`, `outdoor.has_outdoor_seating: true`, `bike_parking: true`

---

### 4. Outdoor

Exterior and natural environment qualities. Essential for parks, hiking, and nature-adjacent queries.

| Attribute | Type | Notes |
|---|---|---|
| `has_outdoor_seating` | boolean | Patio, terrace, or open-air seating |
| `outdoor_heaters` | boolean | Extends outdoor usability in cold weather |
| `scenic_view` | enum | none / street / courtyard / waterfront / skyline / nature / mountain |
| `trail_access_direct` | boolean | Trail reachable without road crossing |
| `trail_difficulty` | enum | none / easy / moderate / hard / expert |
| `shade_available` | boolean | Trees or structures provide shade |
| `green_space_nearby` | boolean | Park within 5-minute walk |
| `sunrise_sunset_viewpoint` | boolean | Unobstructed horizon view |
| `indoor_outdoor_flow` | boolean | Space blurs interior/exterior boundary |

**Example query answered:** *"Outdoor cafe with a view of the skyline where I can bring my dog"*
→ `has_outdoor_seating: true`, `scenic_view: skyline`, `dog_friendly: outdoor_only`

---

### 5. Food & Drink

Beverage and food specifics relevant to extended stays and dietary needs.

| Attribute | Type | Notes |
|---|---|---|
| `specialty_coffee` | boolean | Third-wave, single-origin, or craft espresso |
| `pour_over_available` | boolean | Manual brew methods available |
| `tea_selection` | enum | none / basic / curated / extensive |
| `alcohol_served` | boolean | — |
| `food_menu` | enum | none / snacks_only / light_meals / full_menu |
| `vegan_options` | boolean | — |
| `gluten_free_options` | boolean | — |
| `bring_your_own_food_allowed` | boolean | Can eat your own food here |

---

### 6. Time Sensitivity

When the venue is best — the highest-impact cluster for situational queries ("quiet spot right now").

| Attribute | Type | Notes |
|---|---|---|
| `best_time_of_day` | array | early_morning / morning / midday / afternoon / evening / late_night |
| `best_days` | array | monday–sunday |
| `avoid_during` | string | Free text (e.g., "Friday evenings — packed after 5 PM") |
| `seasonal_quality` | enum | year_round / spring_summer / fall_winter / summer_only / weather_dependent |

**Example query answered:** *"Best quiet coffee shop for a Tuesday morning focus session"*
→ `best_time_of_day: [morning]`, `best_days: [tuesday]`, `noise_level: quiet`, `good_for_deep_work: true`

---

## Scenario Tags

Scenario tags are the highest-level semantic construct in the atmospheric layer. Each tag represents a complete user intent — a reason someone would visit a venue. LLMs match user queries to scenario tags before drilling into specific attributes.

### Full Tag Taxonomy

**Work & Productivity**
- `solo_work_session` — General laptop work, flexible duration
- `focus_sprint` — High-intensity, distraction-free block (2–4 hours)
- `team_offsite` — Small group working session (3–8 people)
- `client_meeting` — Professional meeting with external party
- `job_interview_prep` — Quiet space for rehearsal or calls
- `creative_brainstorm` — Open, stimulating environment for ideation
- `podcast_recording` — Low-noise, acoustically suitable
- `remote_work_tuesday` — Regular recurring work-from-outside day
- `long_stay_nomad` — Full workday (6+ hours), outlet critical
- `quick_laptop_session` — 30–90 minute visit, low commitment

**Social & Leisure**
- `first_date` — Intimate, comfortable, impressive but not intimidating
- `anniversary_dinner` — Special occasion, elevated atmosphere
- `catch_up_with_friend` — Conversational, not too loud, comfortable
- `networking_event` — Standing room, mingling-friendly
- `weekend_brunch` — Leisurely morning-to-noon visit
- `sunset_drinks` — Evening ambiance, outdoor or view preferred
- `family_outing` — Kid and stroller friendly

**Study & Focus Reading**
- `reading_marathon` — Comfortable seating, sustained quiet, no time pressure
- `study_group` — Group study, some conversation acceptable
- `journaling` — Reflective atmosphere, not rushed
- `late_night_writing` — Open late, inspiration-conducive

**Outdoor & Nature**
- `post_hike_recovery` — Near trail end, food/drink available
- `nature_immersion` — Primary experience is natural environment
- `urban_hiking` — Walking-distance venue cluster in a city
- `dog_walk_destination` — Dog-friendly stop on a walk
- `sunrise_session` — Early morning, outdoor, viewpoint
- `photography_spot` — Visually distinctive, worthy of a dedicated visit

**Discovery & Exploration**
- `city_exploration_break` — Rest stop during neighborhood walking
- `people_watching` — Active street frontage, good sightlines
- `rainy_day_retreat` — Indoor, cozy, weather-immune
- `post_gym_refuel` — Adjacent to gym, protein-friendly food options

---

## Confidence Metadata

Every atmospheric record includes a `confidence` object that tells downstream systems how much to trust the data:

| Field | Type | Notes |
|---|---|---|
| `signal_count` | integer | Number of distinct review/source signals used |
| `last_updated` | datetime | ISO 8601 UTC. Stale if >90 days old for active venues |
| `source_diversity` | float 0–1 | Fraction of attributes from 2+ independent sources |
| `human_verified` | boolean | Whether a human editor reviewed these attributes |
| `ai_extracted` | boolean | Whether AI processed review text to populate attributes |

**Trust thresholds:**
- `signal_count < 10` → low confidence, show with caveat
- `signal_count 10–50` → moderate confidence
- `signal_count > 50` → high confidence
- `source_diversity > 0.7` → attributes corroborated across sources
- `last_updated > 90 days` → may be stale, flag for refresh

---

## Relationship to Other Layers

```
/poi-core-schema        ← canonical identity (what it is, where it is)
        ↓
/atmospheric-attributes ← how it feels, what to do there
        ↓
/reviews-sentiment      ← verbatim evidence for specific claims
        ↓
/dynamic-layers         ← is it currently busy, is there an event today
```

Atmospheric attributes are the stable, slowly-changing layer. Dynamic layers change hourly. Reviews sentiment changes weekly. POI core changes rarely.

---

## How AI Models Use This Layer

| User Query | Attributes Matched |
|---|---|
| "quiet cafe for deep work" | `noise_level: quiet`, `good_for_deep_work: true` |
| "dog-friendly outdoor patio" | `dog_friendly: outdoor_only`, `has_outdoor_seating: true` |
| "dimly lit first date spot" | `lighting: dim`, `scenario_tags: first_date` |
| "fast WiFi, lots of outlets, no time limit" | `wifi_quality: excellent`, `power_outlets: abundant`, `time_limit_enforced: false` |
| "cozy place to read on a rainy Sunday" | `interior_style: [cozy]`, `scenario_tags: rainy_day_retreat, reading_marathon`, `best_days: [sunday]` |
| "best time to visit without crowds" | `crowding_typical`, `best_time_of_day`, `avoid_during` |
| "place with a mountain view for sunrise" | `scenic_view: mountain`, `sunrise_sunset_viewpoint: true`, `best_time_of_day: [early_morning]` |

The atmospheric layer is what transforms The Right Spot from a map with pins into a conversational venue intelligence system.
