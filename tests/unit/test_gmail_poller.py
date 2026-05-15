"""Tests for backend/poller/gmail_poller.py."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.db.data_store import DataStore
from backend.engine.status_updater import StatusUpdater
from backend.engine.duplicate_detector import DuplicateDetector
from backend.parser.email_parser import EmailParser
from backend.poller.gmail_poller import GmailPoller, PollerStatus


def _make_poller(tmp_path: Path) -> GmailPoller:
    db = DataStore(db_path=tmp_path / "test.db")
    parser = MagicMock(spec=EmailParser)
    detector = MagicMock(spec=DuplicateDetector)
    updater = MagicMock(spec=StatusUpdater)
    return GmailPoller(db=db, parser=parser, updater=updater)


def test_concurrent_poll_once_skips_second_call(tmp_path: Path) -> None:
    """When poll_once is already running, a second concurrent call returns 0 immediately."""
    poller = _make_poller(tmp_path)

    # Patch _poll_once_locked so the first call blocks long enough for the second to arrive
    barrier = threading.Barrier(2, timeout=3)
    results: list[int] = []
    errors: list[Exception] = []

    original_locked = poller._poll_once_locked

    def slow_poll():
        barrier.wait()   # sync with second call arrival
        time.sleep(0.1)  # hold the lock briefly
        return 3          # pretend 3 messages processed

    poller._poll_once_locked = slow_poll  # type: ignore[method-assign]

    def call_poll(idx: int) -> None:
        try:
            if idx == 0:
                # First caller — acquires lock, blocks inside slow_poll
                r = poller.poll_once()
                results.append(r)
            else:
                barrier.wait()   # arrive after first caller has the lock
                r = poller.poll_once()
                results.append(r)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=call_poll, args=(0,))
    t2 = threading.Thread(target=call_poll, args=(1,))

    # Start first thread (it will acquire the lock and block)
    t1.start()
    time.sleep(0.02)  # let t1 acquire the lock
    t2.start()

    t1.join(timeout=3)
    t2.join(timeout=3)

    assert not errors, f"Unexpected errors: {errors}"
    assert len(results) == 2
    # One call processed messages (3), one was skipped (0)
    assert 0 in results
    assert 3 in results


def test_poll_once_lock_released_on_exception(tmp_path: Path) -> None:
    """Even if _poll_once_locked raises, the lock must be released."""
    poller = _make_poller(tmp_path)

    def failing_poll():
        raise RuntimeError("simulated failure")

    poller._poll_once_locked = failing_poll  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated failure"):
        poller.poll_once()

    # Lock must be free — acquiring it should succeed immediately
    acquired = poller._poll_lock.acquire(blocking=False)
    assert acquired, "Lock was not released after exception"
    poller._poll_lock.release()


def test_initial_status_is_sleeping(tmp_path: Path) -> None:
    poller = _make_poller(tmp_path)
    assert poller.status == PollerStatus.SLEEPING


def test_load_token_from_keyring_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    """Corrupt keyring token returns None without raising."""
    poller = _make_poller(tmp_path)

    with patch("backend.poller.gmail_poller.keyring.get_password", return_value="not-json"):
        result = poller._load_token_from_keyring()
    assert result is None


def test_extract_text_from_payload_returns_empty_when_decode_raises(tmp_path: Path) -> None:
    """If base64.urlsafe_b64decode raises, the function returns '' without propagating."""
    poller = _make_poller(tmp_path)
    import base64
    with patch.object(base64, "urlsafe_b64decode", side_effect=Exception("boom")):
        payload = {"mimeType": "text/plain", "body": {"data": "anythinghere"}}
        result = poller._extract_text_from_payload(payload)
    assert result == ""


def test_extract_text_from_payload_nested_parts(tmp_path: Path) -> None:
    import base64
    poller = _make_poller(tmp_path)
    encoded = base64.urlsafe_b64encode(b"hello from nested").decode()
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/html", "body": {"data": encoded}},
            {"mimeType": "text/plain", "body": {"data": encoded}},
        ],
    }
    result = poller._extract_text_from_payload(payload)
    assert "hello from nested" in result
