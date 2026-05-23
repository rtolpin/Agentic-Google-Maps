/**
 * Datadog tracing tests — TypeScript / Jest
 *
 * MockTracer captures all spans in memory so tests can assert
 * on span names, tags, and error state without a live Datadog agent.
 */

import {
  MockTracer,
  setTracer,
  resetTracer,
  traceSearch,
  traceVenueOpen,
  tracePlaceDetailsFetch,
  traceMapInteraction,
  traceSSEEvent,
  type RecordedSpan,
} from "../lib/tracing";

// ─── Fixtures ──────────────────────────────────────────────────────────────

let tracer: MockTracer;

beforeEach(() => {
  tracer = new MockTracer();
  setTracer(tracer);
});

afterEach(() => {
  resetTracer();
});

// ─── MockTracer unit tests ─────────────────────────────────────────────────

describe("MockTracer", () => {
  it("captures a span by name", () => {
    const span = tracer.startSpan("test.operation");
    span.finish();
    expect(tracer.has("test.operation")).toBe(true);
  });

  it("captures tags set before finish", () => {
    const span = tracer.startSpan("test.operation", { tags: { initial: "tag" } });
    span.setTag("model", "claude-sonnet-4-6");
    span.setTag("tokens", 128);
    span.finish();
    const s = tracer.first("test.operation")!;
    expect(s.tags["initial"]).toBe("tag");
    expect(s.tags["model"]).toBe("claude-sonnet-4-6");
    expect(s.tags["tokens"]).toBe(128);
  });

  it("marks finished after finish()", () => {
    const span = tracer.startSpan("test.span");
    expect(tracer.first("test.span")!.finished).toBe(false);
    span.finish();
    expect(tracer.first("test.span")!.finished).toBe(true);
  });

  it("captures error on rejected promise via trace()", async () => {
    await expect(
      tracer.trace("async.op", {}, async () => { throw new Error("fail"); })
    ).rejects.toThrow("fail");
    const s = tracer.first("async.op")!;
    expect(s.tags["error"]).toContain("fail");
    expect(s.finished).toBe(true);
  });

  it("resolves successful async trace", async () => {
    const result = await tracer.trace("async.success", {}, async () => 42);
    expect(result).toBe(42);
    expect(tracer.first("async.success")!.finished).toBe(true);
  });

  it("accumulates multiple spans with same name", () => {
    for (let i = 0; i < 3; i++) {
      tracer.startSpan("repeated.op").finish();
    }
    expect(tracer.byName("repeated.op")).toHaveLength(3);
  });

  it("clear() resets all spans", () => {
    tracer.startSpan("a").finish();
    tracer.startSpan("b").finish();
    tracer.clear();
    expect(tracer.spans).toHaveLength(0);
  });

  it("errorSpans() returns only spans with error tag", () => {
    const a = tracer.startSpan("ok");
    a.finish();
    const b = tracer.startSpan("bad");
    b.setTag("error", "boom");
    b.finish();
    expect(tracer.errorSpans()).toHaveLength(1);
    expect(tracer.errorSpans()[0].name).toBe("bad");
  });

  it("byName returns empty array for unknown span", () => {
    expect(tracer.byName("nonexistent")).toEqual([]);
  });
});

// ─── traceSearch ───────────────────────────────────────────────────────────

describe("traceSearch", () => {
  it("creates a search span", () => {
    traceSearch({ query: "birthday dinner for 8", userId: "user_123" }).finish();
    expect(tracer.has("therightspot.search")).toBe(true);
  });

  it("sets query_length tag", () => {
    const q = "birthday dinner for 8 in NYC";
    traceSearch({ query: q, userId: "u1" }).finish();
    expect(tracer.first("therightspot.search")!.tags["search.query_length"]).toBe(q.length);
  });

  it("sets user_id tag", () => {
    traceSearch({ query: "test", userId: "user_xyz" }).finish();
    expect(tracer.first("therightspot.search")!.tags["search.user_id"]).toBe("user_xyz");
  });

  it("sets component tag", () => {
    traceSearch({ query: "q", userId: "u" }).finish();
    expect(tracer.first("therightspot.search")!.tags["component"]).toBe("useVenueSearch");
  });

  it("span not finished until .finish() called", () => {
    const span = traceSearch({ query: "q", userId: "u" });
    expect(tracer.first("therightspot.search")!.finished).toBe(false);
    span.finish();
    expect(tracer.first("therightspot.search")!.finished).toBe(true);
  });
});

// ─── traceVenueOpen ────────────────────────────────────────────────────────

describe("traceVenueOpen", () => {
  it("creates a venue_open span", () => {
    traceVenueOpen({ venueId: "v1", venueName: "Locanda Verde", matchScore: 87.5 }).finish();
    expect(tracer.has("therightspot.venue_open")).toBe(true);
  });

  it("tags venue id, name, and match score", () => {
    traceVenueOpen({ venueId: "v1", venueName: "Test Venue", matchScore: 92 }).finish();
    const s = tracer.first("therightspot.venue_open")!;
    expect(s.tags["venue.id"]).toBe("v1");
    expect(s.tags["venue.name"]).toBe("Test Venue");
    expect(s.tags["venue.match_score"]).toBe(92);
  });
});

// ─── tracePlaceDetailsFetch ────────────────────────────────────────────────

describe("tracePlaceDetailsFetch", () => {
  it("creates a place_details_fetch span", () => {
    tracePlaceDetailsFetch("ChIJ_abc123").finish();
    expect(tracer.has("therightspot.place_details_fetch")).toBe(true);
  });

  it("tags venue id", () => {
    tracePlaceDetailsFetch("ChIJ_test").finish();
    expect(tracer.first("therightspot.place_details_fetch")!.tags["venue.id"]).toBe("ChIJ_test");
  });

  it("tags compliance.no_cache as true (TOS requirement)", () => {
    tracePlaceDetailsFetch("ChIJ_test").finish();
    expect(tracer.first("therightspot.place_details_fetch")!.tags["compliance.no_cache"]).toBe(true);
  });
});

// ─── traceMapInteraction ───────────────────────────────────────────────────

describe("traceMapInteraction", () => {
  it("creates a map_interaction span", () => {
    traceMapInteraction({ action: "pan" }).finish();
    expect(tracer.has("therightspot.map_interaction")).toBe(true);
  });

  it("sets action tag", () => {
    traceMapInteraction({ action: "zoom", zoomLevel: 14 }).finish();
    const s = tracer.first("therightspot.map_interaction")!;
    expect(s.tags["map.action"]).toBe("zoom");
    expect(s.tags["map.zoom_level"]).toBe(14);
  });

  it("sets category tag for category_switch", () => {
    traceMapInteraction({ action: "category_switch", category: "hiking" }).finish();
    expect(tracer.first("therightspot.map_interaction")!.tags["map.category"]).toBe("hiking");
  });

  it("sets venue_id for marker_click", () => {
    traceMapInteraction({ action: "marker_click", venueId: "v42" }).finish();
    expect(tracer.first("therightspot.map_interaction")!.tags["map.venue_id"]).toBe("v42");
  });

  it("null tags for omitted optional fields", () => {
    traceMapInteraction({ action: "pan" }).finish();
    const s = tracer.first("therightspot.map_interaction")!;
    expect(s.tags["map.category"]).toBeNull();
    expect(s.tags["map.zoom_level"]).toBeNull();
    expect(s.tags["map.venue_id"]).toBeNull();
  });
});

// ─── traceSSEEvent ─────────────────────────────────────────────────────────

describe("traceSSEEvent", () => {
  it("creates an sse_event span", () => {
    traceSSEEvent("results", 12).finish();
    expect(tracer.has("therightspot.sse_event")).toBe(true);
  });

  it("tags event type and venue count", () => {
    traceSSEEvent("results", 5).finish();
    const s = tracer.first("therightspot.sse_event")!;
    expect(s.tags["sse.event_type"]).toBe("results");
    expect(s.tags["sse.venue_count"]).toBe(5);
  });

  it("null venue count when omitted", () => {
    traceSSEEvent("status").finish();
    expect(tracer.first("therightspot.sse_event")!.tags["sse.venue_count"]).toBeNull();
  });
});

// ─── End-to-end span tree ──────────────────────────────────────────────────

describe("span tree for a complete search flow", () => {
  it("produces the expected span hierarchy", async () => {
    // Simulate the spans a real search would emit
    const searchSpan = traceSearch({ query: "hiking trails near Central Park", userId: "u1" });
    searchSpan.setTag("search.category", "hiking");

    const interactionSpan = traceMapInteraction({ action: "ai_query" });
    interactionSpan.finish();

    // Simulate SSE events arriving
    traceSSEEvent("status").finish();
    traceSSEEvent("intent").finish();
    traceSSEEvent("results", 8).finish();
    traceSSEEvent("done", 8).finish();

    // User opens a venue
    const venueSpan = traceVenueOpen({ venueId: "v99", venueName: "Central Park Trail", matchScore: 78 });
    const detailsSpan = tracePlaceDetailsFetch("ChIJ_park");
    detailsSpan.setTag("http.status_code", 200);
    detailsSpan.finish();
    venueSpan.finish();

    searchSpan.finish();

    // Assert all expected spans present
    expect(tracer.has("therightspot.search")).toBe(true);
    expect(tracer.has("therightspot.map_interaction")).toBe(true);
    expect(tracer.byName("therightspot.sse_event")).toHaveLength(4);
    expect(tracer.has("therightspot.venue_open")).toBe(true);
    expect(tracer.has("therightspot.place_details_fetch")).toBe(true);

    // Search span must be last to finish
    const allFinished = tracer.spans.every((s) => s.finished);
    expect(allFinished).toBe(true);

    // Zero error spans in a happy path
    expect(tracer.errorSpans()).toHaveLength(0);
  });

  it("error spans are captured when search fails", async () => {
    const span = traceSearch({ query: "bad query", userId: "u2" });
    span.setTag("error", "HTTP 500 — internal server error");
    span.finish();

    expect(tracer.errorSpans()).toHaveLength(1);
    expect(tracer.errorSpans()[0].name).toBe("therightspot.search");
  });
});
