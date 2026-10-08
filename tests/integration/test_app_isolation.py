"""Regression tests: starting the app under test never touches Gmail, the keychain, Google
APIs, the poller, or the user's real database — and the isolation guards themselves work.

Background: the app lifespan used to build GmailPoller, call authenticate() (keychain →
Google token refresh) and start PollerScheduler, whose first iteration polls Gmail
immediately. Every TestClient(app) therefore imported real mail.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

import backend.poller.gmail_poller as gmail_poller_module
import backend.poller.scheduler as scheduler_module
import backend.poller.sleep_watcher as sleep_watcher_module
from backend import config
from backend.main import app
from tests import isolation

REPO_ROOT = Path(__file__).resolve().parents[2]

# Databases that belong to the user and must never be created or modified by tests: the
# default location for this checkout, and the original checkout named in CLAUDE.md.
USER_DATABASES = (
    REPO_ROOT / ".job-tracker" / "applications.db",
    Path.home() / "Codes" / "job-tracker" / ".job-tracker" / "applications.db",
)


def _fingerprint(path: Path) -> tuple[bool, int | None, int | None]:
    """Existence, size and mtime — read via stat only, never opened."""
    if not path.exists():
        return (False, None, None)
    stat = path.stat()
    return (True, stat.st_size, stat.st_mtime_ns)


def test_test_environment_is_isolated() -> None:
    assert config.POLLER_ENABLED is False
    assert config.CACHE_ENABLED is False
    assert config.LLM_ENABLED is False
    assert config.APP_ENV == "test"
    assert config.JOB_TRACKER_DIR == isolation.TEST_JOB_TRACKER_DIR
    assert config.DB_PATH.is_relative_to(isolation.TEST_JOB_TRACKER_DIR)
    for user_db in USER_DATABASES:
        assert config.DB_PATH != user_db
    import os

    assert "GMAIL_TOKEN_JSON" not in os.environ
    assert "GROQ_API_KEY" not in os.environ


def test_app_startup_never_touches_gmail_keychain_poller_or_user_database() -> None:
    before = {p: _fingerprint(p) for p in USER_DATABASES}
    spies = {
        "keyring.get_password": MagicMock(name="keyring.get_password"),
        "GmailPoller.authenticate": MagicMock(name="authenticate"),
        "GmailPoller.authenticate_headless": MagicMock(name="authenticate_headless"),
        "Credentials.from_authorized_user_info": MagicMock(name="from_authorized_user_info"),
        "InstalledAppFlow.from_client_secrets_file": MagicMock(name="InstalledAppFlow"),
        "googleapiclient build": MagicMock(name="build"),
        "build_poller": MagicMock(name="build_poller"),
        "PollerScheduler.start": MagicMock(name="PollerScheduler.start"),
        "SleepWatcher.start": MagicMock(name="SleepWatcher.start"),
    }
    with (
        patch("keyring.get_password", spies["keyring.get_password"]),
        patch.object(
            gmail_poller_module.GmailPoller, "authenticate", spies["GmailPoller.authenticate"]
        ),
        patch.object(
            gmail_poller_module.GmailPoller,
            "authenticate_headless",
            spies["GmailPoller.authenticate_headless"],
        ),
        patch.object(
            gmail_poller_module.Credentials,
            "from_authorized_user_info",
            spies["Credentials.from_authorized_user_info"],
        ),
        patch.object(
            gmail_poller_module.InstalledAppFlow,
            "from_client_secrets_file",
            spies["InstalledAppFlow.from_client_secrets_file"],
        ),
        patch.object(gmail_poller_module, "build", spies["googleapiclient build"]),
        patch("googleapiclient.discovery.build", spies["googleapiclient build"]),
        patch.object(scheduler_module, "build_poller", spies["build_poller"]),
        patch.object(scheduler_module.PollerScheduler, "start", spies["PollerScheduler.start"]),
        patch.object(sleep_watcher_module.SleepWatcher, "start", spies["SleepWatcher.start"]),
    ):
        with TestClient(app) as client:
            assert app.state.poller_scheduler is None
            assert app.state.db is not None
            assert client.get("/api/v1/health").status_code == 200
            assert not any(t.name == "poller" for t in threading.enumerate())

    called = {name: spy.call_count for name, spy in spies.items() if spy.called}
    assert called == {}, f"startup touched external integrations: {called}"
    assert isolation.keyring_attempts == []
    assert isolation.network_attempts == []
    assert {p: _fingerprint(p) for p in USER_DATABASES} == before


def test_poller_enabled_startup_is_unchanged(monkeypatch) -> None:
    """Outside tests (POLLER_ENABLED=true, the default) the lifespan still authenticates and
    starts the scheduler — verified here with everything Gmail-related mocked."""
    monkeypatch.setattr("backend.config.POLLER_ENABLED", True)
    fake_poller = MagicMock(name="GmailPoller")
    with (
        patch.object(scheduler_module, "build_poller", return_value=fake_poller) as build,
        patch.object(scheduler_module.PollerScheduler, "start") as start,
        patch.object(scheduler_module.PollerScheduler, "stop") as stop,
    ):
        with TestClient(app):
            assert app.state.poller_scheduler is not None
            assert app.state.poller_scheduler.poller is fake_poller
    build.assert_called_once()
    fake_poller.authenticate.assert_called_once()
    start.assert_called_once()
    stop.assert_called_once()


def test_poller_disabled_reports_credentials_without_reading_them(monkeypatch) -> None:
    get_password = MagicMock()
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setenv("GMAIL_TOKEN_JSON", '{"refresh_token": "x"}')
    report = gmail_poller_module.GmailPoller.describe_credentials()
    assert report.source == "disabled"
    get_password.assert_not_called()


# ------------------------------------------------------------------ #
# The guards themselves                                                #
# ------------------------------------------------------------------ #


def test_external_connection_is_blocked_and_recorded() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(isolation.UnexpectedNetworkAccess):
            sock.connect(("203.0.113.10", 443))  # TEST-NET-3, never routable
    finally:
        sock.close()
    assert isolation.network_attempts == ["connect ('203.0.113.10', 443)"]
    isolation.network_attempts.clear()  # handled: this test asserts the violation itself


def test_external_dns_lookup_is_blocked_and_recorded() -> None:
    with pytest.raises(isolation.UnexpectedNetworkAccess):
        socket.getaddrinfo("gmail.googleapis.com", 443)
    assert isolation.network_attempts == ["resolve 'gmail.googleapis.com'"]
    isolation.network_attempts.clear()


def test_http_client_to_google_is_blocked() -> None:
    import requests

    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get("https://oauth2.googleapis.com/token", timeout=2)
    assert isolation.network_attempts
    isolation.network_attempts.clear()


def test_loopback_connections_are_allowed() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(server.getsockname())
        assert socket.getaddrinfo("localhost", 80)
        assert socket.getaddrinfo("jobtracker.localhost", 80)
    finally:
        client.close()
        server.close()
    assert isolation.network_attempts == []


def test_unmocked_keychain_access_is_refused_and_recorded() -> None:
    import keyring
    import keyring.errors

    with pytest.raises(keyring.errors.NoKeyringError):
        keyring.get_password(config.GMAIL_KEYCHAIN_SERVICE, config.GMAIL_KEYCHAIN_USERNAME)
    assert isolation.keyring_attempts == [f"get {config.GMAIL_KEYCHAIN_SERVICE}"]
    isolation.keyring_attempts.clear()
