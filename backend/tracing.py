"""
Datadog APM distributed tracing for The Right Spot API.

All instrumentation flows through this module.  Tests replace `tracer`
with a SpanRecorder so no real Datadog agent is needed.

Span hierarchy produced by a single search request:
  therightspot.search
  ├── therightspot.parse_intent         (AI — intent classification)
  ├── therightspot.scrape               (HTTP — Nimble SERP + maps)
  │   ├── therightspot.nimble_maps      (HTTP — google_maps engine)
  │   └── therightspot.nimble_serp      (HTTP — google organic, ×3)
  ├── therightspot.extract_signals      (AI — Claude signal extractor, ×N)
  ├── therightspot.score_venues         (DB — ClickHouse score query)
  ├── therightspot.synthesize           (AI — why-card + scenario, ×3)
  ├── therightspot.senso.query_kb       (HTTP — Senso knowledge base)
  ├── therightspot.senso.publish        (HTTP — Senso content publish)
  └── therightspot.google_maps.details  (HTTP — Places API real-time)
"""
from __future__ import annotations

import contextlib
from contextlib import contextmanager
from typing import Any, Generator, Iterator

from ddtrace import Span
from ddtrace import tracer as _dd_tracer
from ddtrace.ext import SpanTypes

# ─── Service constants ────────────────────────────────────────────────────────

SERVICE_API = "therightspot-api"
SERVICE_AI = "therightspot-ai"
SERVICE_DB = "therightspot-db"
SERVICE_MAPS = "therightspot-maps"
SERVICE_SENSO = "therightspot-senso"
VERSION = "2.0.0"

# Replaceable in tests — see SpanRecorder below
tracer = _dd_tracer


# ─── Span context managers ────────────────────────────────────────────────────

@contextmanager
def ai_span(
    operation: str,
    model: str = "claude-sonnet-4-6",
    **tags: Any,
) -> Iterator[Span]:
    """
    Trace an AI agent operation (intent parsing, signal extraction, synthesis).
    Tags: model, tokens.input, tokens.output, cache.hit.
    """
    with tracer.trace(
        operation,
        service=SERVICE_AI,
        resource=operation,
        span_type=SpanTypes.LLM,
    ) as span:
        span.set_tag("ai.model", model)
        span.set_tag("version", VERSION)
        for k, v in tags.items():
            if v is not None:
                span.set_tag(k, v)
        yield span


@contextmanager
def db_span(
    operation: str,
    table: str,
    **tags: Any,
) -> Iterator[Span]:
    """
    Trace a ClickHouse database operation.
    Tags: db.table, db.rows_returned, db.query_type.
    """
    with tracer.trace(
        operation,
        service=SERVICE_DB,
        resource=f"{table}.{operation.split('.')[-1]}",
        span_type=SpanTypes.SQL,
    ) as span:
        span.set_tag("db.type", "clickhouse")
        span.set_tag("db.table", table)
        for k, v in tags.items():
            if v is not None:
                span.set_tag(k, v)
        yield span


@contextmanager
def http_span(
    operation: str,
    service_name: str,
    url: str = "",
    method: str = "POST",
    **tags: Any,
) -> Iterator[Span]:
    """
    Trace an external HTTP call (Nimble, Google Maps, Senso).
    Tags: http.url, http.method, http.status_code, http.service.
    """
    with tracer.trace(
        operation,
        service=SERVICE_API,
        resource=f"{method} {service_name}",
        span_type=SpanTypes.HTTP,
    ) as span:
        span.set_tag("http.url", url)
        span.set_tag("http.method", method)
        span.set_tag("http.service", service_name)
        for k, v in tags.items():
            if v is not None:
                span.set_tag(k, v)
        yield span


@contextmanager
def search_span(query: str, user_id: str) -> Iterator[Span]:
    """Root span for an end-to-end search request."""
    with tracer.trace(
        "therightspot.search",
        service=SERVICE_API,
        resource="search",
        span_type="web",
    ) as span:
        span.set_tag("search.query_length", len(query))
        span.set_tag("search.user_id", user_id)
        span.set_tag("version", VERSION)
        yield span


# ─── Test utilities ───────────────────────────────────────────────────────────

class RecordedSpan:
    """Lightweight span stub captured by SpanRecorder."""

    def __init__(self, name: str, service: str, resource: str, span_type: str) -> None:
        self.name = name
        self.service = service
        self.resource = resource
        self.span_type = span_type
        self.tags: dict[str, Any] = {}
        self.error: int = 0
        self.finished = False
        self._children: list["RecordedSpan"] = []

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[key] = value

    def set_metric(self, key: str, value: float) -> None:
        self.tags[f"_metrics.{key}"] = value

    def finish(self) -> None:
        self.finished = True

    def __enter__(self) -> "RecordedSpan":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            self.error = 1
            self.set_tag("error.type", exc_type.__name__)
            self.set_tag("error.message", str(exc_val))
        self.finish()
        return False  # don't suppress exceptions

    def __repr__(self) -> str:
        return f"RecordedSpan({self.name!r}, tags={self.tags})"


class SpanRecorder:
    """
    Drop-in replacement for the ddtrace global tracer in unit tests.
    Replace `tracing.tracer` with an instance of this class to capture
    all spans without a live Datadog agent.

    Usage::

        import therightspot.tracing as tracing_module

        recorder = SpanRecorder()
        monkeypatch.setattr(tracing_module, "tracer", recorder)

        # run code under test ...

        spans = recorder.by_name("therightspot.parse_intent")
        assert len(spans) == 1
        assert spans[0].tags["ai.model"] == "claude-sonnet-4-6"
    """

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def trace(
        self,
        name: str,
        service: str = "",
        resource: str = "",
        span_type: str = "",
        **_: Any,
    ) -> Iterator[RecordedSpan]:
        span = RecordedSpan(name, service, resource, span_type)
        self.spans.append(span)
        try:
            yield span
        except Exception as exc:
            span.error = 1
            span.set_tag("error.type", type(exc).__name__)
            span.set_tag("error.message", str(exc))
            raise
        finally:
            span.finish()

    # ── Query helpers ─────────────────────────────────────────────────────────

    def by_name(self, name: str) -> list[RecordedSpan]:
        return [s for s in self.spans if s.name == name]

    def first(self, name: str) -> RecordedSpan | None:
        spans = self.by_name(name)
        return spans[0] if spans else None

    def has(self, name: str) -> bool:
        return any(s.name == name for s in self.spans)

    def error_spans(self) -> list[RecordedSpan]:
        return [s for s in self.spans if s.error]

    def clear(self) -> None:
        self.spans.clear()

    def __len__(self) -> int:
        return len(self.spans)
