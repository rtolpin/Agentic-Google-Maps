"""
ClickHouse client — venue signals store, scoring engine, and city benchmarks.

Engine choices:
  venue_signals    → ReplacingMergeTree   (dedup by scraped_at, partition by city)
  city_benchmarks  → AggregatingMergeTree (pre-aggregated rollups, fast panel reads)
  user_sessions    → MergeTree            (append-only, partitioned by month for cheap TTL)

All public methods are synchronous; callers that run in async context must wrap
them in asyncio.to_thread().  Never mark these async — clickhouse-connect has no
native async driver and false async def blocks the event loop.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import clickhouse_connect

from models.models import CityBenchmark, ScoredVenue, VenueIntent, VenueSignal

# ─── Connection config ────────────────────────────────────────────────────────

_CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
_CH_PORT = int(os.environ.get("CLICKHOUSE_PORT", 8123))
_CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
_CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
_CH_DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "rightspot")
# ClickHouse Cloud uses port 8443 with TLS — auto-detect
_CH_SECURE = _CH_PORT == 8443 or os.environ.get("CLICKHOUSE_SECURE", "").lower() == "true"

# ─── DDL ──────────────────────────────────────────────────────────────────────

_DDL = """
CREATE DATABASE IF NOT EXISTS rightspot;

-- Core venue signals
-- ReplacingMergeTree deduplicates by (city, cuisine, venue_id), keeping the row
-- with the latest scraped_at.  Partition by city keeps city-scoped queries fast.
--
-- place_id: Google Place ID extracted by Nimble google_maps engine.
--   Only this identifier is stored — never Google's content data (ratings, photos).
--   The frontend uses it with the Google Maps JS API for map rendering.
-- address / latitude / longitude: extracted by Nimble or geocoded via Google Maps API.
--   Coordinates are our data (universal WGS84 values), not Google content.
CREATE TABLE IF NOT EXISTS rightspot.venue_signals (
    venue_id             String,
    name                 String,
    city                 LowCardinality(String),
    neighborhood         String,
    cuisine              LowCardinality(String),
    url                  String,
    place_id             String DEFAULT '',
    address              String DEFAULT '',
    latitude             Float32 DEFAULT 0,
    longitude            Float32 DEFAULT 0,
    noise_level          Enum8('very_quiet'=1,'quiet'=2,'moderate'=3,'loud'=4,'very_loud'=5),
    has_private_room     UInt8,
    max_group_size       UInt8,
    birthday_score       UInt8,
    wifi_quality         Enum8('none'=0,'poor'=1,'good'=2,'excellent'=3),
    dog_friendly         UInt8,
    outdoor_seating      UInt8,
    price_per_head       UInt16,
    booking_difficulty   Enum8('easy'=1,'moderate'=2,'hard'=3),
    special_occasion_score UInt8,
    birthday_mentions    UInt16,
    key_quotes           Array(String),
    scraped_at           DateTime DEFAULT now(),
    signal_age_hrs       UInt16 DEFAULT 0
) ENGINE = ReplacingMergeTree(scraped_at)
  PARTITION BY city
  ORDER BY (city, cuisine, venue_id)
  TTL scraped_at + INTERVAL 30 DAY
  SETTINGS index_granularity = 8192;

-- Migration for existing tables: add columns if not present
ALTER TABLE rightspot.venue_signals ADD COLUMN IF NOT EXISTS place_id String DEFAULT '';
ALTER TABLE rightspot.venue_signals ADD COLUMN IF NOT EXISTS address String DEFAULT '';
ALTER TABLE rightspot.venue_signals ADD COLUMN IF NOT EXISTS latitude Float32 DEFAULT 0;
ALTER TABLE rightspot.venue_signals ADD COLUMN IF NOT EXISTS longitude Float32 DEFAULT 0;

-- Pre-aggregated city benchmarks for the global insight panel.
-- AggregatingMergeTree keeps running state functions so re-aggregating is O(1).
CREATE TABLE IF NOT EXISTS rightspot.city_benchmarks (
    city                       LowCardinality(String),
    occasion                   LowCardinality(String),
    cuisine                    LowCardinality(String),
    avg_special_occasion_score AggregateFunction(avg, Float32),
    avg_price_per_head         AggregateFunction(avg, Float32),
    avg_private_room_rate      AggregateFunction(avg, Float32),
    venue_count                AggregateFunction(count, UInt32),
    updated_at                 DateTime DEFAULT now()
) ENGINE = AggregatingMergeTree()
  ORDER BY (city, occasion, cuisine);

-- User sessions — append-only, cheap monthly TTL via partition pruning.
CREATE TABLE IF NOT EXISTS rightspot.user_sessions (
    user_id        String,
    query          String,
    selected_venue String,
    feedback       Int8,
    session_at     DateTime DEFAULT now(),
    intent_json    String
) ENGINE = MergeTree()
  PARTITION BY toYYYYMM(session_at)
  ORDER BY (user_id, session_at)
  TTL session_at + INTERVAL 365 DAY;
"""

# ─── Scoring query ────────────────────────────────────────────────────────────
# Freshness computed live via dateDiff so signal_age_hrs never needs updating.
# Occasion-specific bonus rewards birthday_score when appropriate.

_SCORE_QUERY = """
SELECT
    venue_id,
    name,
    city,
    neighborhood,
    cuisine,
    place_id,
    address,
    latitude,
    longitude,
    price_per_head,
    has_private_room,
    max_group_size,
    noise_level,
    birthday_score,
    key_quotes,
    scraped_at,
    LEAST(100, GREATEST(0,
        -- Base: any venue passing city+cuisine+freshness filters is a real candidate
        40

        -- Group capacity (0-20 pts): partial credit when size unknown (stored as 0)
        + multiIf(
            max_group_size = 0,        10,
            max_group_size >= {group_size:UInt8}, 20,
            greatest(0, toInt32(20) * max_group_size / greatest(1, toInt32({group_size:UInt8})))
          )

        -- Noise match (0-15 pts)
        + multiIf(
            {noise_pref:String} = 'quiet',
                CASE noise_level
                    WHEN 'very_quiet' THEN 15
                    WHEN 'quiet'      THEN 12
                    WHEN 'moderate'   THEN 5
                    ELSE 0 END,
            {noise_pref:String} = 'lively',
                CASE noise_level
                    WHEN 'loud'       THEN 15
                    WHEN 'very_loud'  THEN 12
                    WHEN 'moderate'   THEN 7
                    ELSE 3 END,
            CASE noise_level
                WHEN 'moderate' THEN 12
                WHEN 'quiet'    THEN 9
                WHEN 'loud'     THEN 9
                ELSE 6 END
          )

        -- Occasion fit (0-20 pts): higher weights + partial credit when score is 0
        + multiIf(
            {occasion:String} IN ('birthday_dinner', 'birthday_party'),
                multiIf(birthday_score > 0, birthday_score * 0.20, 8),
            multiIf(special_occasion_score > 0, special_occasion_score * 0.20, 8)
          )

        -- Price band match (0-10 pts): partial credit when price unknown
        + multiIf(
            price_per_head = 0, 5,
            price_per_head BETWEEN {price_min:UInt16} AND {price_max:UInt16}, 10,
            0
          )

        -- Private room bonus (0-5 pts)
        + multiIf({needs_private_room:Bool}, has_private_room * 5, 0)

        -- Freshness penalty: capped at -5 pts (1 pt per day, max 5 days)
        - LEAST(5, dateDiff('hour', scraped_at, now()) / 24)
    )) AS match_score
FROM rightspot.venue_signals
FINAL
WHERE (city = {city:String} OR {city:String} = 'Unknown')
  AND (cuisine = {cuisine:String} OR {cuisine:String} = '')
  AND scraped_at >= now() - INTERVAL 7 DAY
ORDER BY match_score DESC
LIMIT 20
"""


# ─── Client ───────────────────────────────────────────────────────────────────

class ClickHouseClient:
    def __init__(self) -> None:
        self._conn: Any = None  # lazy — connect on first use

    def _connect(self) -> None:
        """Bootstrap: create the rightspot database if it doesn't exist, then connect."""
        bootstrap = clickhouse_connect.get_client(
            host=_CH_HOST,
            port=_CH_PORT,
            username=_CH_USER,
            password=_CH_PASSWORD,
            secure=_CH_SECURE,
        )
        bootstrap.command(f"CREATE DATABASE IF NOT EXISTS {_CH_DATABASE}")
        bootstrap.close()
        self._conn = clickhouse_connect.get_client(
            host=_CH_HOST,
            port=_CH_PORT,
            username=_CH_USER,
            password=_CH_PASSWORD,
            database=_CH_DATABASE,
            secure=_CH_SECURE,
        )

    @property
    def client(self) -> Any:
        if getattr(self, "_conn", None) is None:
            self._connect()
        return self._conn

    @client.setter
    def client(self, value: Any) -> None:
        self._conn = value

    # ── Schema ────────────────────────────────────────────────────────────────

    def initialize_schema(self) -> None:
        """Create all tables if they don't exist. Idempotent."""
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self.client.command(stmt)

    # ── Write path ────────────────────────────────────────────────────────────

    def upsert_venue_signals(self, venues: list[dict], city: str) -> None:
        """
        Insert freshly scraped venue signals.
        Serialization delegates to VenueSignal.to_ch_row() — no hardcoded field lists.
        """
        if not venues:
            return
        now = datetime.utcnow()
        rows: list[list] = []
        for raw in venues:
            try:
                # Enrich with city so venue_id is scoped correctly
                raw["city"] = raw.get("city") or city
                signal = VenueSignal.model_validate(raw)
                rows.append(signal.to_ch_row(now))
            except Exception:
                continue  # skip malformed records; don't abort the batch
        if rows:
            self.client.insert(
                "venue_signals",
                rows,
                column_names=VenueSignal.CH_COLUMNS,
            )

    def record_session(
        self,
        user_id: str,
        query: str,
        intent: dict,
        selected: str = "",
        feedback: int = 0,
    ) -> None:
        self.client.insert(
            "user_sessions",
            [[user_id, query, selected, feedback, datetime.utcnow(), json.dumps(intent)]],
            column_names=["user_id", "query", "selected_venue",
                          "feedback", "session_at", "intent_json"],
        )

    # ── Read path ─────────────────────────────────────────────────────────────

    def score_venues(self, intent: VenueIntent) -> list[ScoredVenue]:
        """
        Execute the weighted scoring query and return typed ScoredVenue objects.
        Parameters are derived entirely from the VenueIntent model.
        """
        result = self.client.query(_SCORE_QUERY, parameters=intent.to_score_params())
        return [ScoredVenue.from_ch_row(row) for row in result.result_rows]

    def get_cached_scores(self, city: str, cuisine: str) -> list[ScoredVenue]:
        """Return venues scored within the last 30 minutes (warm-cache shortcut)."""
        result = self.client.query(
            """
            SELECT venue_id, name, city, neighborhood, cuisine,
                   place_id, address, latitude, longitude,
                   price_per_head, has_private_room, max_group_size,
                   noise_level, birthday_score, key_quotes, scraped_at,
                   special_occasion_score AS match_score
            FROM rightspot.venue_signals FINAL
            WHERE city = {city:String}
              AND (cuisine = {cuisine:String} OR {cuisine:String} = '')
              AND scraped_at >= now() - INTERVAL 30 MINUTE
            ORDER BY match_score DESC
            LIMIT 5
            """,
            parameters={"city": city, "cuisine": cuisine},
        )
        return [ScoredVenue.from_ch_row(row) for row in result.result_rows]

    def get_map_markers(self, city: str, venue_ids: list[str] | None = None) -> list[ScoredVenue]:
        """
        Return venues that have a Google Place ID, for frontend map rendering.
        Callers convert the result with GoogleMapsClient.to_map_markers().
        Only place_id + coordinates + display fields are needed — no signals.
        """
        where = "city = {city:String} AND place_id != ''"
        params: dict = {"city": city}
        if venue_ids:
            where += " AND venue_id IN {ids:Array(String)}"
            params["ids"] = venue_ids
        result = self.client.query(
            f"""
            SELECT venue_id, name, city, neighborhood, cuisine,
                   place_id, address, latitude, longitude,
                   price_per_head, has_private_room, max_group_size,
                   noise_level, birthday_score, key_quotes, scraped_at,
                   0 AS match_score
            FROM rightspot.venue_signals FINAL
            WHERE {where}
            LIMIT 50
            """,
            parameters=params,
        )
        return [ScoredVenue.from_ch_row(row) for row in result.result_rows]

    def get_city_benchmarks(
        self, cities: list[str], occasion: str
    ) -> dict[str, CityBenchmark]:
        """Fetch global city comparison data for the insight panel."""
        result = self.client.query(
            """
            SELECT
                city,
                avgMerge(avg_special_occasion_score),
                avgMerge(avg_price_per_head),
                avgMerge(avg_private_room_rate),
                countMerge(venue_count)
            FROM rightspot.city_benchmarks
            WHERE city IN {cities:Array(String)}
              AND occasion = {occasion:String}
            GROUP BY city
            """,
            parameters={"cities": cities, "occasion": occasion},
        )
        return {
            row[0]: CityBenchmark(
                occasion_score=round(float(row[1]), 1),
                avg_price=round(float(row[2]), 0),
                private_room_rate=round(float(row[3]), 2),
                venue_count=int(row[4]),
            )
            for row in result.result_rows
        }

    def get_venue_by_id(self, venue_id: str) -> dict | None:
        """Fetch a single venue's full signal record."""
        result = self.client.query(
            "SELECT * FROM rightspot.venue_signals FINAL WHERE venue_id = {id:String}",
            parameters={"id": venue_id},
        )
        if not result.result_rows:
            return None
        cols = [c.name for c in result.column_names] if hasattr(result, "column_names") else []
        row = result.result_rows[0]
        return dict(zip(cols, row)) if cols else {"row": row}
