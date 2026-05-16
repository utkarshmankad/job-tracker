"""Tests for backend/poller/poll_once_cli.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.poller.poll_once_cli import _load_headless_credentials, poll_once


# ---------------------------------------------------------------------------
# _load_headless_credentials
# ---------------------------------------------------------------------------


def _valid_token_json() -> str:
    return json.dumps(
        {
            "token": "ya29.fake",
            "refresh_token": "1//fake-refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake-client-id.apps.googleusercontent.com",
            "client_secret": "fake-secret",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        }
    )


def test_load_headless_credentials_returns_none_when_no_keyring_entry() -> None:
    with patch("backend.poller.poll_once_cli.keyring.get_password", return_value=None):
        result = _load_headless_credentials()
    assert result is None


def test_load_headless_credentials_returns_none_on_corrupt_json() -> None:
    with patch("backend.poller.poll_once_cli.keyring.get_password", return_value="not-json"):
        result = _load_headless_credentials()
    assert result is None


def test_load_headless_credentials_refreshes_expired_token() -> None:
    mock_creds = MagicMock()
    mock_creds.expired = True
    mock_creds.refresh_token = "1//fake"
    mock_creds.valid = True

    with patch("backend.poller.poll_once_cli.keyring.get_password", return_value=_valid_token_json()), \
         patch("backend.poller.poll_once_cli.Credentials.from_authorized_user_info", return_value=mock_creds), \
         patch("backend.poller.poll_once_cli.keyring.set_password") as mock_set:
        result = _load_headless_credentials()

    assert result is mock_creds
    mock_creds.refresh.assert_called_once()
    mock_set.assert_called_once()


def test_load_headless_credentials_returns_none_when_refresh_fails() -> None:
    mock_creds = MagicMock()
    mock_creds.expired = True
    mock_creds.refresh_token = "1//fake"
    mock_creds.valid = False
    mock_creds.refresh.side_effect = Exception("network error")

    with patch("backend.poller.poll_once_cli.keyring.get_password", return_value=_valid_token_json()), \
         patch("backend.poller.poll_once_cli.Credentials.from_authorized_user_info", return_value=mock_creds):
        result = _load_headless_credentials()

    assert result is None


# ---------------------------------------------------------------------------
# poll_once
# ---------------------------------------------------------------------------


def test_poll_once_exits_2_when_no_credentials() -> None:
    with patch("backend.poller.poll_once_cli._load_headless_credentials", return_value=None), \
         pytest.raises(SystemExit) as exc_info:
        poll_once()
    assert exc_info.value.code == 2


def test_poll_once_dry_run_exits_cleanly(capsys) -> None:
    mock_creds = MagicMock()
    mock_creds.valid = True
    with patch("backend.poller.poll_once_cli._load_headless_credentials", return_value=mock_creds):
        result = poll_once(dry_run=True)
    assert result == 0
    captured = capsys.readouterr()
    assert "Auth OK" in captured.out


def test_poll_once_returns_count_on_success(tmp_path: Path, capsys) -> None:
    mock_creds = MagicMock()
    mock_creds.valid = True

    mock_poller = MagicMock()
    mock_poller.poll_once.return_value = 3

    with patch("backend.poller.poll_once_cli._load_headless_credentials", return_value=mock_creds), \
         patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller), \
         patch("backend.poller.poll_once_cli._build") as mock_build:
        mock_build.return_value = MagicMock()
        count = poll_once()

    assert count == 3
    captured = capsys.readouterr()
    assert "3 new application(s)" in captured.out


def test_poll_once_exits_2_on_auth_error(tmp_path: Path) -> None:
    from backend.poller.error_retry import AuthError

    mock_creds = MagicMock()
    mock_creds.valid = True

    mock_poller = MagicMock()
    mock_poller.poll_once.side_effect = AuthError("401")

    with patch("backend.poller.poll_once_cli._load_headless_credentials", return_value=mock_creds), \
         patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller), \
         patch("backend.poller.poll_once_cli._build"), \
         pytest.raises(SystemExit) as exc_info:
        poll_once()
    assert exc_info.value.code == 2


def test_poll_once_exits_1_on_http_error(tmp_path: Path) -> None:
    from googleapiclient.errors import HttpError

    mock_creds = MagicMock()
    mock_creds.valid = True

    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_poller = MagicMock()
    mock_poller.poll_once.side_effect = HttpError(mock_resp, b"server error")

    with patch("backend.poller.poll_once_cli._load_headless_credentials", return_value=mock_creds), \
         patch("backend.poller.poll_once_cli.build_poller", return_value=mock_poller), \
         patch("backend.poller.poll_once_cli._build"), \
         pytest.raises(SystemExit) as exc_info:
        poll_once()
    assert exc_info.value.code == 1
