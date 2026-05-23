"""
Redis cache — intent cache (1hr TTL), session memory, and user preferences.

Uses redis.asyncio so cache operations never block the event loop.
Gracefully degrades to a no-op when Redis is unavailable.
"""
from __future__ import annotations

import json
import os
from typing import Any

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
_USER_PREFS_PREFIX = "user_prefs:"


class RedisCache:
    """
    Async Redis client with graceful no-op fallback when Redis is unavailable.
    All methods are safe to call even if the Redis server is down.
    """

    def __init__(self) -> None:
        self._client: Any = None
        if _REDIS_AVAILABLE:
            self._client = aioredis.from_url(
                _REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )

    async def get(self, key: str) -> str | None:
        if self._client is None:
            return None
        try:
            return await self._client.get(key)
        except Exception:
            return None

    async def set(self, key: str, value: str, ttl: int = 3600) -> None:
        if self._client is None:
            return
        try:
            await self._client.set(key, value, ex=ttl)
        except Exception:
            pass

    async def get_user_prefs(self, user_id: str) -> dict | None:
        """Return stored user preferences dict, or None if not found."""
        raw = await self.get(f"{_USER_PREFS_PREFIX}{user_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def set_user_prefs(self, user_id: str, prefs: dict) -> None:
        """Persist user preferences with a 30-day TTL."""
        await self.set(
            f"{_USER_PREFS_PREFIX}{user_id}",
            json.dumps(prefs),
            ttl=30 * 24 * 3600,
        )

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
