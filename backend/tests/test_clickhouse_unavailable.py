"""
Regression: ClickHouse being unreachable (e.g. a deleted Cloud service → DNS
NXDOMAIN) must not take the API down. Previously the lifespan hook raised,
so every request — including CORS preflights — returned a bare 500.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ..api.server import app
from ..agents.orchestrator import orchestrate


def _dead_ch() -> MagicMock:
    ch = MagicMock()
    err = ConnectionError("Failed to resolve 'dead.clickhouse.cloud'")
    for name in ("initialize_schema", "get_cached_scores", "upsert_venue_signals", "score_venues"):
        getattr(ch, name).side_effect = err
    return ch


def test_app_starts_and_answers_preflight_without_clickhouse():
    with patch("backend.api.server._ch", _dead_ch()):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            resp = client.options(
                "/api/search/stream",
                headers={
                    "Origin": "https://agent-google-maps-nine.vercel.app",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://agent-google-maps-nine.vercel.app"


@pytest.mark.asyncio
async def test_search_falls_back_to_in_memory_scoring_without_clickhouse(birthday_intent, mock_redis):
    scraped = [{
        "name": "Locanda Verde", "venue_id": "lv", "city": "New York City",
        "latitude": 40.72, "longitude": -74.01, "price_per_head": 95,
        "noise_level": "quiet", "key_quotes": [], "google_rating": 4.6,
    }]
    with (
        patch("backend.agents.orchestrator._ch", _dead_ch()),
        patch("backend.agents.orchestrator._cache", mock_redis),
        patch("backend.agents.orchestrator.parse_intent", AsyncMock(return_value=birthday_intent)),
        patch("backend.agents.orchestrator.ScraperAgent") as scraper,
        patch("backend.agents.orchestrator.ValidatorAgent") as validator,
        patch("backend.agents.orchestrator.GlobalIntelligenceAgent") as global_agent,
        patch("backend.agents.orchestrator.GoogleMapsClient") as maps,
        patch("backend.agents.orchestrator.synthesize_venue_intelligence", AsyncMock(side_effect=RuntimeError)),
        patch("backend.agents.orchestrator.PublisherAgent"),
    ):
        scraper.return_value.run = AsyncMock(return_value=scraped)
        validator.return_value.run = AsyncMock(return_value={})
        global_agent.return_value.run = AsyncMock(return_value={})
        maps.return_value.__aenter__ = AsyncMock(return_value=MagicMock(geocode=AsyncMock(return_value=None)))
        maps.return_value.__aexit__ = AsyncMock(return_value=None)
        events = [e async for e in orchestrate("birthday dinner", "u1")]

    names = [e["event"] for e in events]
    assert "error" not in names
    results = next(e["data"] for e in events if e["event"] == "results")
    assert [v["name"] for v in results] == ["Locanda Verde"]
