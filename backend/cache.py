"""Redis-backed response cache for hot read endpoints.

Fails open by design: any Redis connectivity problem degrades to "no cache" rather
than a broken request. The first failed operation disables caching for the rest of
the process's lifetime (no repeated connect-timeout cost on every subsequent request
when Redis simply isn't running, e.g. local dev without `redis-server`, or CI).
"""

from __future__ import annotations

import json
from typing import Any

import redis
import structlog

from backend.config import CACHE_CONNECT_TIMEOUT_SECONDS, CACHE_ENABLED, REDIS_URL

log = structlog.get_logger(__name__)

_client: redis.Redis | None = None
_disabled = not CACHE_ENABLED


def _get_client() -> redis.Redis | None:
    global _client, _disabled
    if _disabled:
        return None
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            socket_connect_timeout=CACHE_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=CACHE_CONNECT_TIMEOUT_SECONDS,
        )
    return _client


def _disable(reason: str, error: Exception) -> None:
    global _disabled
    _disabled = True
    log.warning("cache_disabled", reason=reason, error=str(error))


def get_json(key: str) -> Any | None:
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except redis.RedisError as exc:
        _disable("get_failed", exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.set(key, json.dumps(value), ex=ttl_seconds)
    except redis.RedisError as exc:
        _disable("set_failed", exc)


def invalidate_prefix(prefix: str) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=f"{prefix}*", count=200)
            if keys:
                client.delete(*keys)
            if cursor == 0:
                break
    except redis.RedisError as exc:
        _disable("invalidate_failed", exc)
