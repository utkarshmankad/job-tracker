"""Tests for backend/poller/error_retry.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from backend.poller.error_retry import AuthError, RateLimitError, gmail_retry


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp, b"error body")


def test_successful_call_passes_through() -> None:
    @gmail_retry()
    def fn():
        return "ok"

    assert fn() == "ok"


def test_401_raises_auth_error_immediately() -> None:
    calls = {"n": 0}

    @gmail_retry(max_attempts=3)
    def fn():
        calls["n"] += 1
        raise _http_error(401)

    with pytest.raises(AuthError):
        fn()
    assert calls["n"] == 1, "401 must not be retried"


def test_403_raises_auth_error_immediately() -> None:
    @gmail_retry(max_attempts=3)
    def fn():
        raise _http_error(403)

    with pytest.raises(AuthError):
        fn()


def test_non_retryable_http_error_propagates_unwrapped() -> None:
    @gmail_retry(max_attempts=3)
    def fn():
        raise _http_error(404)

    with pytest.raises(HttpError):
        fn()


def test_429_exhausts_retries_and_reraises_rate_limit_error() -> None:
    calls = {"n": 0}

    @gmail_retry(max_attempts=1)
    def fn():
        calls["n"] += 1
        raise _http_error(429)

    with pytest.raises(RateLimitError):
        fn()
    assert calls["n"] == 1


def test_429_then_success_returns_result() -> None:
    calls = {"n": 0}

    @gmail_retry(max_attempts=3)
    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429)
        return "recovered"

    assert fn() == "recovered"
    assert calls["n"] == 2
