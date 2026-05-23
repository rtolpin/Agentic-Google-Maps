"""
Unit tests for the FastAPI server — SSE stream, WebSocket, feedback, and global API.
Uses httpx.AsyncClient (TestClient) against the ASGI app.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from ..api.server import app
from ..models.models import CityBenchmark, ScoredVenue, VenueIntelligence


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_orch_events(events: list[dict]):
    """Return an async generator that yields SSE event dicts."""
    async def _gen(*_args, **_kwargs):
        for e in events:
            yield e
    return _gen


# ─── Health ───────────────────────────────────────────────────────────────────

class TestHealth:
    @pytest.mark.asyncio
    async def test_returns_ok(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ─── /api/search/stream ───────────────────────────────────────────────────────

class TestSearchStream:
    @pytest.mark.asyncio
    async def test_returns_200_and_event_stream(self, mock_ch):
        events = [
            {"event": "status", "data": "Searching..."},
            {"event": "results", "data": []},
            {"event": "done", "data": {"total_venues": 0}},
        ]

        with (
            patch("files.server._ch", mock_ch),
            patch("files.server.orchestrate", _make_orch_events(events)),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                async with client.stream(
                    "POST",
                    "/api/search/stream",
                    json={"query": "birthday dinner NYC", "user_id": "u1"},
                ) as resp:
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_streams_all_events(self, mock_ch):
        events = [
            {"event": "status", "data": "Parsing..."},
            {"event": "intent", "data": {"city": "NYC"}},
            {"event": "done", "data": {"total_venues": 3}},
        ]

        received: list[dict] = []

        with (
            patch("files.server._ch", mock_ch),
            patch("files.server.orchestrate", _make_orch_events(events)),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                async with client.stream(
                    "POST",
                    "/api/search/stream",
                    json={"query": "test", "user_id": "u1"},
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            received.append(json.loads(line[6:]))

        event_types = [r["event"] for r in received if "event" in r]
        assert "status" in event_types
        assert "intent" in event_types
        assert "end" in event_types  # always emitted in finally block

    @pytest.mark.asyncio
    async def test_error_in_orchestrator_yields_error_event(self, mock_ch):
        async def failing_orch(*_args, **_kwargs):
            raise RuntimeError("Scraper timeout")
            yield  # make it a generator

        received: list[dict] = []

        with (
            patch("files.server._ch", mock_ch),
            patch("files.server.orchestrate", failing_orch),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                async with client.stream(
                    "POST",
                    "/api/search/stream",
                    json={"query": "test"},
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            received.append(json.loads(line[6:]))

        error_events = [r for r in received if r.get("event") == "error"]
        assert len(error_events) == 1
        assert "Scraper timeout" in error_events[0]["data"]

    @pytest.mark.asyncio
    async def test_user_id_auto_assigned_when_absent(self, mock_ch):
        events = [{"event": "done", "data": {"total_venues": 0}}]
        captured_user_id = None

        async def capture_user_id(query: str, user_id: str):
            nonlocal captured_user_id
            captured_user_id = user_id
            for e in events:
                yield e

        with (
            patch("files.server._ch", mock_ch),
            patch("files.server.orchestrate", capture_user_id),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                async with client.stream(
                    "POST",
                    "/api/search/stream",
                    json={"query": "test"},  # no user_id
                ) as resp:
                    async for _ in resp.aiter_lines():
                        pass

        assert captured_user_id is not None
        assert len(captured_user_id) > 0


# ─── /api/feedback ────────────────────────────────────────────────────────────

class TestFeedback:
    @pytest.mark.asyncio
    async def test_records_feedback_and_returns_status(self, mock_ch):
        with patch("files.server._ch", mock_ch):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/feedback",
                    json={
                        "user_id": "u1",
                        "venue_id": "v1",
                        "query": "birthday dinner NYC",
                        "feedback": 1,
                    },
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "recorded"

    @pytest.mark.asyncio
    async def test_invalid_feedback_value_returns_422(self, mock_ch):
        with patch("files.server._ch", mock_ch):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/feedback",
                    json={
                        "user_id": "u1",
                        "venue_id": "v1",
                        "query": "test",
                        "feedback": 5,  # invalid
                    },
                )
        assert resp.status_code == 422


# ─── /api/global/{city} ───────────────────────────────────────────────────────

class TestGlobalBenchmarks:
    @pytest.mark.asyncio
    async def test_returns_city_benchmarks(self, mock_ch):
        mock_ch.get_city_benchmarks.return_value = {
            "New York City": CityBenchmark(
                occasion_score=82.5, avg_price=95.0, private_room_rate=0.45, venue_count=1200
            )
        }

        with patch("files.server._ch", mock_ch):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/global/New%20York%20City?occasion=birthday_dinner")

        assert resp.status_code == 200
        data = resp.json()
        assert "New York City" in data


# ─── /api/venue/{venue_id}/signals ────────────────────────────────────────────

class TestVenueSignals:
    @pytest.mark.asyncio
    async def test_returns_venue_data(self, mock_ch):
        mock_ch.get_venue_by_id.return_value = {"venue_id": "v1", "name": "Test"}

        with patch("files.server._ch", mock_ch):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/venue/v1/signals")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_returns_404_when_not_found(self, mock_ch):
        mock_ch.get_venue_by_id.return_value = None

        with patch("files.server._ch", mock_ch):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/venue/nonexistent/signals")

        assert resp.status_code == 404
