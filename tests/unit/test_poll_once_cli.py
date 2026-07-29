"""Tests for backend/poller/poll_once_cli.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.poller.poll_once_cli import poll_once


def _mock_poller(authenticated: bool) -> MagicMock:
    mock_poller = MagicMock()
    mock_poller.authenticate_headless.return_value = authenticated
    return mock_poller


def test_poll_once_exits_2_when_no_credentials() -> None:
    mock_poller = _mock_poller(authenticated=False)
    with patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller), \
         pytest.raises(SystemExit) as exc_info:
        poll_once()
    assert exc_info.value.code == 2


def test_poll_once_dry_run_exits_cleanly(capsys) -> None:
    mock_poller = _mock_poller(authenticated=True)
    with patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller):
        result = poll_once(dry_run=True)
    assert result == 0
    captured = capsys.readouterr()
    assert "Auth OK" in captured.out
    mock_poller.poll_once.assert_not_called()


def test_poll_once_returns_count_on_success(tmp_path: Path, capsys) -> None:
    mock_poller = _mock_poller(authenticated=True)
    mock_poller.poll_once.return_value = 3

    with patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller):
        count = poll_once()

    assert count == 3
    captured = capsys.readouterr()
    assert "3 new application(s)" in captured.out


def test_poll_once_exits_2_on_auth_error(tmp_path: Path) -> None:
    from backend.poller.error_retry import AuthError

    mock_poller = _mock_poller(authenticated=True)
    mock_poller.poll_once.side_effect = AuthError("401")

    with patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller), \
         pytest.raises(SystemExit) as exc_info:
        poll_once()
    assert exc_info.value.code == 2


def test_poll_once_exits_1_on_http_error(tmp_path: Path) -> None:
    from googleapiclient.errors import HttpError

    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_poller = _mock_poller(authenticated=True)
    mock_poller.poll_once.side_effect = HttpError(mock_resp, b"server error")

    with patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller), \
         pytest.raises(SystemExit) as exc_info:
        poll_once()
    assert exc_info.value.code == 1
