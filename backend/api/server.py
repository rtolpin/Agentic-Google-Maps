"""
FastAPI server — SSE streaming search, WebSocket, and REST endpoints.
ClickHouse operations run in a thread pool via asyncio.to_thread so they
never block the event loop.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

# Load .env from project root (two levels up from backend/api/)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parents[2] / ".env")
except ImportError:
    pass
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from ..agents.orchestrator import orchestrate
from ..db.clickhouse import ClickHouseClient
from ..integrations.google_maps_client import GoogleMapsClient
from ..models.models import (
    CityBenchmark,
    FeedbackSignal,
    GooglePlaceDetails,
    HealthResponse,
    MapMarker,
    SearchRequest,
)

_ch = ClickHouseClient()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize schema on startup; clean up on shutdown."""
    await asyncio.to_thread(_ch.initialize_schema)
    yield


app = FastAPI(title="The Right Spot API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://therightspot.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


# ─── Search (SSE) ─────────────────────────────────────────────────────────────

@app.post("/api/search/stream")
async def search_stream(req: SearchRequest) -> StreamingResponse:
    """
    Server-Sent Events endpoint.
    Each event is a JSON object: {"event": "<type>", "data": <payload>}.
    The frontend consumes with a ReadableStream (not EventSource) so it can
    pass an Authorization header and read partial results as they arrive.
    """
    user_id = req.user_id or str(uuid.uuid4())

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for result in orchestrate(req.query, user_id):
                yield f"data: {json.dumps(result, default=str)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'event': 'error', 'data': str(exc)})}\n\n"
        finally:
            yield 'data: {"event": "end"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str) -> None:
    """Bi-directional WebSocket: client sends queries, server streams results."""
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            async for result in orchestrate(payload["query"], user_id):
                await websocket.send_text(json.dumps(result, default=str))
    except WebSocketDisconnect:
        pass


# ─── Feedback ─────────────────────────────────────────────────────────────────

@app.post("/api/feedback")
async def record_feedback(req: FeedbackSignal) -> dict:
    """Record a thumbs-up / thumbs-down for personalization learning."""
    await asyncio.to_thread(
        _ch.record_session,
        req.user_id,
        req.query,
        {},
        req.venue_id,
        req.feedback,
    )
    return {"status": "recorded"}


# ─── Global benchmarks ────────────────────────────────────────────────────────

@app.get("/api/global/{city}", response_model=dict[str, CityBenchmark])
async def get_global_benchmarks(
    city: str, occasion: str = "dinner"
) -> dict[str, CityBenchmark]:
    """Return global city comparison benchmarks for the insight panel."""
    cities = list({"New York City", "Rome", "Tokyo", "Paris", "London", city})
    return await asyncio.to_thread(_ch.get_city_benchmarks, cities, occasion)


# ─── Venue detail ─────────────────────────────────────────────────────────────

@app.get("/api/venue/{venue_id}/signals")
async def get_venue_signals(venue_id: str) -> dict:
    """Return all scraped signals for a specific venue."""
    row = await asyncio.to_thread(_ch.get_venue_by_id, venue_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Venue not found")
    return row


# ─── Google Maps — real-time place details ────────────────────────────────────
# COMPLIANCE: Google TOS requires place details to be displayed on a Google Map
# and forbids long-term caching.  This endpoint fetches live from the Places API
# on every call — it is intentionally NOT cached.

@app.get(
    "/api/venue/{venue_id}/place",
    response_model=GooglePlaceDetails,
    summary="Real-time Google Place details (not cached)",
)
async def get_place_details(venue_id: str) -> GooglePlaceDetails:
    """
    Fetch live place details from the Google Maps Platform Places API.
    Call this when the user opens a venue card to show ratings, hours, and phone.
    Result is NOT stored — display on a Google Map per TOS requirements.
    """
    row = await asyncio.to_thread(_ch.get_venue_by_id, venue_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Venue not found")

    place_id = row.get("place_id", "")
    if not place_id:
        raise HTTPException(
            status_code=422,
            detail="No Google Place ID on record for this venue. "
                   "Run a fresh search to populate it via Nimble.",
        )

    maps = GoogleMapsClient()
    try:
        details = await maps.get_place_details(place_id)
    finally:
        await maps.close()

    if details is None:
        raise HTTPException(status_code=404, detail="Place ID not found in Google Maps")
    return details


# ─── Google Maps — map markers ────────────────────────────────────────────────

@app.get(
    "/api/map/markers",
    response_model=list[MapMarker],
    summary="Map marker payloads for Google Maps JS API",
)
async def get_map_markers(city: str) -> list[MapMarker]:
    """
    Return minimal map marker data for all venues in a city that have a Place ID.

    The frontend passes these place_ids to the Google Maps JavaScript API to
    render interactive pins.  Only our own data (scores, names, coordinates)
    is included — no cached Google content.

    COMPLIANCE: Markers here use Google Place IDs sourced from Nimble extraction.
    Display them on an official Google Map (Maps JS API) to comply with TOS.
    """
    venues = await asyncio.to_thread(_ch.get_map_markers, city)
    return GoogleMapsClient.to_map_markers(venues)
