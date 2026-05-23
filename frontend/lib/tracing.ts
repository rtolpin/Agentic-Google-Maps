/**
 * Datadog APM + Browser RUM tracing for The Right Spot.
 *
 * Server-side (Next.js API routes / Node.js):  dd-trace
 * Client-side (React / browser):               @datadog/browser-rum
 *
 * All instrumentation flows through this module so tests can
 * inject a MockTracer without touching production code.
 */

// ─── Types ─────────────────────────────────────────────────────────────────

export interface SpanOptions {
  tags?: Record<string, string | number | boolean | null>;
}

export interface TracingSpan {
  setTag(key: string, value: string | number | boolean | null): void;
  finish(): void;
}

export interface Tracer {
  startSpan(operation: string, options?: SpanOptions): TracingSpan;
  trace<T>(operation: string, options: SpanOptions, fn: (span: TracingSpan) => T): T;
}

export interface SearchTracePayload {
  query: string;
  userId: string;
}

export interface VenueOpenTracePayload {
  venueId: string;
  venueName: string;
  matchScore: number;
}

export interface MapInteractionPayload {
  action: "pan" | "zoom" | "marker_click" | "category_switch" | "ai_query";
  category?: string;
  zoomLevel?: number;
  venueId?: string;
}

// ─── Mock Tracer (for tests) ───────────────────────────────────────────────

export interface RecordedSpan {
  name: string;
  tags: Record<string, string | number | boolean | null>;
  finished: boolean;
  error?: string;
}

export class MockTracer implements Tracer {
  readonly spans: RecordedSpan[] = [];

  startSpan(operation: string, options: SpanOptions = {}): TracingSpan {
    const recorded: RecordedSpan = {
      name: operation,
      tags: { ...options.tags },
      finished: false,
    };
    this.spans.push(recorded);

    return {
      setTag(key: string, value: string | number | boolean | null): void {
        recorded.tags[key] = value;
      },
      finish(): void {
        recorded.finished = true;
      },
    };
  }

  trace<T>(operation: string, options: SpanOptions, fn: (span: TracingSpan) => T): T {
    const span = this.startSpan(operation, options);
    try {
      const result = fn(span);
      if (result instanceof Promise) {
        return result.then(
          (v) => { span.finish(); return v; },
          (e) => { span.setTag("error", String(e)); span.finish(); throw e; },
        ) as unknown as T;
      }
      span.finish();
      return result;
    } catch (e) {
      span.setTag("error", String(e));
      span.finish();
      throw e;
    }
  }

  byName(name: string): RecordedSpan[] {
    return this.spans.filter((s) => s.name === name);
  }

  first(name: string): RecordedSpan | undefined {
    return this.byName(name)[0];
  }

  has(name: string): boolean {
    return this.spans.some((s) => s.name === name);
  }

  errorSpans(): RecordedSpan[] {
    return this.spans.filter((s) => s.tags["error"] != null);
  }

  clear(): void {
    this.spans.length = 0;
  }
}

// ─── Production Tracer (wraps dd-trace or no-op in browser) ───────────────

class NoopSpan implements TracingSpan {
  setTag(_key: string, _value: unknown): void {}
  finish(): void {}
}

class NoopTracer implements Tracer {
  startSpan(_operation: string, _options?: SpanOptions): TracingSpan {
    return new NoopSpan();
  }
  trace<T>(_operation: string, _options: SpanOptions, fn: (span: TracingSpan) => T): T {
    return fn(new NoopSpan());
  }
}

/**
 * Server-side tracer backed by dd-trace.
 * Loaded lazily so the browser bundle never imports Node.js modules.
 */
class DdTracer implements Tracer {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private _dd: any;

  constructor() {
    try {
      // Dynamic require so bundlers skip this in browser builds
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      this._dd = require("dd-trace");
      this._dd.init({
        service: "therightspot-frontend",
        version: "2.0.0",
        env: process.env.NODE_ENV,
        analytics: true,
      });
    } catch {
      this._dd = null;
    }
  }

  startSpan(operation: string, options: SpanOptions = {}): TracingSpan {
    if (!this._dd) return new NoopSpan();
    const span = this._dd.startSpan(operation, { tags: options.tags ?? {} });
    return {
      setTag(key: string, value: string | number | boolean | null): void {
        span.setTag(key, value);
      },
      finish(): void {
        span.finish();
      },
    };
  }

  trace<T>(operation: string, options: SpanOptions, fn: (span: TracingSpan) => T): T {
    if (!this._dd) return fn(new NoopSpan());
    return this._dd.trace(operation, { tags: options.tags ?? {} }, (ddSpan: unknown) => {
      const wrapped: TracingSpan = {
        setTag(key: string, value: string | number | boolean | null): void {
          (ddSpan as { setTag: (k: string, v: unknown) => void }).setTag(key, value);
        },
        finish(): void {
          (ddSpan as { finish: () => void }).finish();
        },
      };
      return fn(wrapped);
    }) as T;
  }
}

// ─── Active tracer (injectable) ────────────────────────────────────────────

let _activeTracer: Tracer = typeof window === "undefined"
  ? new DdTracer()
  : new NoopTracer();  // Browser RUM handles client-side tracing

export function getTracer(): Tracer {
  return _activeTracer;
}

/** Replace the active tracer — used in tests. */
export function setTracer(t: Tracer): void {
  _activeTracer = t;
}

/** Reset to the production tracer. */
export function resetTracer(): void {
  _activeTracer = typeof window === "undefined"
    ? new DdTracer()
    : new NoopTracer();
}

// ─── Instrumented operations ───────────────────────────────────────────────

/** Trace a venue search request (SSE stream open). */
export function traceSearch(payload: SearchTracePayload): TracingSpan {
  return getTracer().startSpan("therightspot.search", {
    tags: {
      "search.query_length": payload.query.length,
      "search.user_id": payload.userId,
      "component": "useVenueSearch",
    },
  });
}

/** Trace a user opening a venue detail card. */
export function traceVenueOpen(payload: VenueOpenTracePayload): TracingSpan {
  return getTracer().startSpan("therightspot.venue_open", {
    tags: {
      "venue.id": payload.venueId,
      "venue.name": payload.venueName,
      "venue.match_score": payload.matchScore,
    },
  });
}

/** Trace a Google Place details fetch (real-time, TOS-required display). */
export function tracePlaceDetailsFetch(venueId: string): TracingSpan {
  return getTracer().startSpan("therightspot.place_details_fetch", {
    tags: {
      "venue.id": venueId,
      "compliance.no_cache": true,
    },
  });
}

/** Trace a map interaction (pan, zoom, marker click, AI query). */
export function traceMapInteraction(payload: MapInteractionPayload): TracingSpan {
  return getTracer().startSpan("therightspot.map_interaction", {
    tags: {
      "map.action": payload.action,
      "map.category": payload.category ?? null,
      "map.zoom_level": payload.zoomLevel ?? null,
      "map.venue_id": payload.venueId ?? null,
    },
  });
}

/** Trace an SSE event received from the backend. */
export function traceSSEEvent(eventType: string, venueCount?: number): TracingSpan {
  return getTracer().startSpan("therightspot.sse_event", {
    tags: {
      "sse.event_type": eventType,
      "sse.venue_count": venueCount ?? null,
    },
  });
}

// ─── Browser RUM custom actions ────────────────────────────────────────────

/**
 * Send a custom action to Datadog Browser RUM.
 * Safe to call in the browser; no-op in Node.js.
 */
export function rumAction(name: string, context?: Record<string, unknown>): void {
  if (typeof window === "undefined") return;
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const dd = (window as any).DD_RUM;
    dd?.addAction(name, context);
  } catch {
    // RUM not initialised — silently skip
  }
}
