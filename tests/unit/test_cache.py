"""Tests for backend/cache.py — must fail open on any Redis error."""

from __future__ import annotations

from unittest.mock import MagicMock

import redis as redis_module

from backend import cache


def _reset_cache_module(monkeypatch) -> None:
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_disabled", False)


def test_get_json_returns_none_when_cache_disabled(monkeypatch):
    monkeypatch.setattr(cache, "_disabled", True)
    assert cache.get_json("some:key") is None


def test_set_json_noop_when_cache_disabled(monkeypatch):
    monkeypatch.setattr(cache, "_disabled", True)
    mock_client = MagicMock()
    monkeypatch.setattr(cache, "_client", mock_client)
    cache.set_json("some:key", {"a": 1}, 30)
    mock_client.set.assert_not_called()


def test_get_json_round_trips_through_mocked_client(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.get.return_value = b'{"a": 1}'
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    result = cache.get_json("some:key")

    assert result == {"a": 1}


def test_get_json_returns_none_on_cache_miss(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.get.return_value = None
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    assert cache.get_json("missing:key") is None


def test_get_json_returns_none_on_corrupt_json(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.get.return_value = b"not json"
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    assert cache.get_json("some:key") is None


def test_get_json_disables_cache_on_redis_error(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.get.side_effect = redis_module.RedisError("connection refused")
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    result = cache.get_json("some:key")

    assert result is None
    assert cache._disabled is True


def test_set_json_calls_client_set_with_ttl(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    cache.set_json("some:key", {"a": 1}, 30)

    mock_client.set.assert_called_once()
    args, kwargs = mock_client.set.call_args
    assert args[0] == "some:key"
    assert kwargs["ex"] == 30


def test_set_json_disables_cache_on_redis_error(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.set.side_effect = redis_module.RedisError("connection refused")
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    cache.set_json("some:key", {"a": 1}, 30)

    assert cache._disabled is True


def test_invalidate_prefix_deletes_matching_keys(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.scan.side_effect = [(1, [b"applications:list:a"]), (0, [b"applications:list:b"])]
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    cache.invalidate_prefix("applications:list:")

    assert mock_client.delete.call_count == 2


def test_invalidate_prefix_noop_when_no_matches(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.scan.return_value = (0, [])
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    cache.invalidate_prefix("applications:list:")

    mock_client.delete.assert_not_called()


def test_invalidate_prefix_disables_cache_on_redis_error(monkeypatch):
    _reset_cache_module(monkeypatch)
    mock_client = MagicMock()
    mock_client.scan.side_effect = redis_module.RedisError("connection refused")
    monkeypatch.setattr(cache, "_get_client", lambda: mock_client)

    cache.invalidate_prefix("applications:list:")

    assert cache._disabled is True


def test_get_client_returns_none_when_permanently_disabled(monkeypatch):
    monkeypatch.setattr(cache, "_disabled", True)
    assert cache._get_client() is None


def test_get_client_builds_and_caches_client_instance(monkeypatch):
    _reset_cache_module(monkeypatch)
    fake_client = MagicMock()
    monkeypatch.setattr(
        redis_module.Redis, "from_url", classmethod(lambda cls, *a, **k: fake_client)
    )

    first = cache._get_client()
    second = cache._get_client()

    assert first is fake_client
    assert second is fake_client
