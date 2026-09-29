"""
Per-client sliding-window rate limiter for the LLM-backed endpoints.

Every search fans out into ~12+ Claude calls (intent, up to N signal
extractions, 10 syntheses, guide), so an unthrottled client can burn API
budget fast. This limiter is in-process: on multi-instance deployments
(e.g. Vercel) each instance enforces its own window, which still bounds
per-instance cost. Put a shared limiter (Redis / edge) in front for a hard
global cap.
"""
from __future__ import annotations

import os
import time
from collections import OrderedDict, deque

from fastapi import Request


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_s: float, max_keys: int = 10_000) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Record a hit for key. Returns (allowed, retry_after_seconds)."""
        if self.max_requests <= 0:
            return True, 0.0
        now = time.monotonic() if now is None else now
        hits = self._hits.pop(key, None) or deque()
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        allowed = len(hits) < self.max_requests
        if allowed:
            hits.append(now)
        self._hits[key] = hits  # re-insert as most recently used
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
        retry_after = 0.0 if allowed else max(0.0, self.window_s - (now - hits[0]))
        return allowed, retry_after

    def reset(self) -> None:
        self._hits.clear()


def client_key(request: Request) -> str:
    """Best-effort client identity: first X-Forwarded-For hop (Vercel/proxy), else socket peer."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


search_limiter = SlidingWindowRateLimiter(
    max_requests=int(os.environ.get("SEARCH_RATE_LIMIT_PER_MIN", "20")),
    window_s=60.0,
)
