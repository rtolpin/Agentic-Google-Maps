"""
Global Intelligence Agent — city-scale benchmarks for the insight panel.

Fetches pre-aggregated ClickHouse city_benchmarks data and returns
a comparison dict yielded as {"event": "global_intel", "data": ...}.
Runs in parallel with ScraperAgent and ValidatorAgent.
"""
from __future__ import annotations

import asyncio

from ..db.clickhouse import ClickHouseClient
from ..models.models import VenueIntent

_COMPARISON_CITIES = ["New York City", "Rome", "Tokyo", "Paris", "London"]


class GlobalIntelligenceAgent:
    """Fetches global city benchmarks so the frontend can show comparative context."""

    async def run(self, intent: VenueIntent) -> dict:
        cities = list({*_COMPARISON_CITIES, intent.city})
        try:
            ch = ClickHouseClient()
            benchmarks = await asyncio.to_thread(
                ch.get_city_benchmarks, cities, intent.occasion
            )
        except Exception:
            benchmarks = {}

        city_data = {
            city: bm.model_dump() for city, bm in benchmarks.items()
        }

        current = city_data.get(intent.city)
        avg_price = (
            current["avg_price"] if current else None
        )

        return {
            "city": intent.city,
            "occasion": intent.occasion,
            "benchmarks": city_data,
            "avg_price_in_city": avg_price,
            "comparison_cities": [c for c in cities if c != intent.city],
        }
