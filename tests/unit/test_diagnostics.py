"""Unit tests for backend/diagnostics.py.

Mirrors backend/diagnostics.py per the project test convention.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.diagnostics import DiagnosticResult, DiagnosticRunner

# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "diag_test.db"


@pytest.fixture
def db(db_path: Path) -> DataStore:
    return DataStore(db_path)


@pytest.fixture
def runner(db: DataStore, db_path: Path) -> DiagnosticRunner:
    _ = db  # ensure DB is initialised first
    return DiagnosticRunner(db_path=db_path)


# ------------------------------------------------------------------ #
# DiagnosticResult                                                     #
# ------------------------------------------------------------------ #


def test_diagnostic_result_str_ok():
    r = DiagnosticResult(name="test_check", ok=True, detail="all good")
    assert "[✓]" in str(r)
    assert "test_check" in str(r)
    assert "all good" in str(r)


def test_diagnostic_result_str_fail():
    r = DiagnosticResult(name="bad_check", ok=False, detail="something broke")
    assert "[✗]" in str(r)
    assert "bad_check" in str(r)


# ------------------------------------------------------------------ #
# _check_db_file                                                       #
# ------------------------------------------------------------------ #


def test_check_db_file_exists(runner: DiagnosticRunner):
    result = runner._check_db_file()
    assert result.ok
    assert "Exists" in result.detail


def test_check_db_file_missing(tmp_path: Path):
    missing_path = tmp_path / "nonexistent.db"
    runner = DiagnosticRunner(db_path=missing_path)
    result = runner._check_db_file()
    assert not result.ok
    assert "not found" in result.detail.lower()


# ------------------------------------------------------------------ #
# _check_db_connectivity                                               #
# ------------------------------------------------------------------ #


def test_check_db_connectivity_healthy(runner: DiagnosticRunner):
    result = runner._check_db_connectivity()
    assert result.ok
    assert "Connected" in result.detail


def test_check_db_connectivity_missing_db_is_reported_not_created(tmp_path: Path):
    # Diagnostics must never create (or migrate) a database — a missing file is a failure.
    missing = tmp_path / "new.db"
    runner = DiagnosticRunner(db_path=missing)
    result = runner._check_db_connectivity()
    assert not result.ok
    assert "not found" in result.detail
    assert not missing.exists()


# ------------------------------------------------------------------ #
# _check_schema_tables                                                 #
# ------------------------------------------------------------------ #


def test_check_schema_tables_complete(runner: DiagnosticRunner):
    result = runner._check_schema_tables()
    assert result.ok
    assert "present" in result.detail


def test_check_schema_tables_missing(tmp_path: Path):
    # Create an empty SQLite file (no tables).
    empty_db = tmp_path / "empty.db"
    sqlite3.connect(str(empty_db)).close()
    runner = DiagnosticRunner(db_path=empty_db)
    result = runner._check_schema_tables()
    assert not result.ok
    assert "Missing" in result.detail


# ------------------------------------------------------------------ #
# _check_enum_values — the core correctness check                      #
# ------------------------------------------------------------------ #


def _insert_raw_status(db_path: Path, value: str) -> None:
    """Bypass the ORM and insert a row with an arbitrary current_status string."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    now = utc_now().isoformat()
    cur.execute(
        """
        INSERT INTO application
            (company, role, source_portal, applied_date,
             current_status, thread_ids, is_false_positive, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("Acme", "Dev", "LinkedIn", now, value, "[]", 0, now, now),
    )
    conn.commit()
    conn.close()


def test_check_enum_values_empty_db(runner: DiagnosticRunner):
    result = runner._check_enum_values()
    assert result.ok
    assert "0" in result.detail  # zero distinct values


def test_check_enum_values_all_correct(db: DataStore, db_path: Path):
    from datetime import UTC
    from datetime import datetime as dt

    app = Application(
        company="X",
        source_portal="LinkedIn",
        applied_date=dt(2026, 1, 1, tzinfo=UTC),
        current_status=ApplicationStatus.APPLIED,
        updated_at=dt(2026, 1, 1, tzinfo=UTC),
    )
    db.upsert_application(app)

    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_enum_values()
    assert result.ok, result.detail


def test_check_enum_values_name_format_detected(db: DataStore, db_path: Path):
    """Old DBs may store 'APPLIED' (member name) instead of 'Applied' (value).
    The diagnostic must detect this and report it as an error.
    """
    # Use DataStore to create the schema, then bypass ORM to insert NAME-format data.
    _insert_raw_status(db_path, "APPLIED")

    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_enum_values()
    assert not result.ok
    assert "APPLIED" in result.detail
    assert "LookupError" in result.detail


def test_check_enum_values_unknown_value_detected(db: DataStore, db_path: Path):
    _insert_raw_status(db_path, "UNKNOWN_STATUS")
    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_enum_values()
    assert not result.ok
    assert "UNKNOWN_STATUS" in result.detail


def test_check_enum_values_mixed_formats_detected(db: DataStore, db_path: Path):
    # One row with correct value format, one row with old name format.
    _insert_raw_status(db_path, "Applied")  # correct
    _insert_raw_status(db_path, "REJECTED")  # old name format

    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_enum_values()
    assert not result.ok
    assert "REJECTED" in result.detail


# ------------------------------------------------------------------ #
# _check_poller_state                                                  #
# ------------------------------------------------------------------ #


def test_check_poller_state_sleeping(runner: DiagnosticRunner):
    result = runner._check_poller_state()
    assert result.ok
    assert "SLEEPING" in result.detail


def test_check_poller_state_auth_error(db: DataStore, db_path: Path):
    db.update_poller_state(status="AUTH_ERROR", error_message="Token expired")
    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_poller_state()
    assert not result.ok
    assert "reauth" in result.detail.lower()


def test_check_poller_state_api_error(db: DataStore, db_path: Path):
    db.update_poller_state(status="API_ERROR", error_message="quota exceeded")
    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_poller_state()
    assert not result.ok
    assert "quota exceeded" in result.detail


def test_check_poller_state_stale(db: DataStore, db_path: Path):
    stale_time = utc_now() - timedelta(minutes=20)
    db.update_poller_state(status="SLEEPING", last_sync_at=stale_time)
    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_poller_state()
    assert not result.ok
    assert "min ago" in result.detail


def test_check_poller_state_recent(db: DataStore, db_path: Path):
    db.update_poller_state(status="RUNNING", last_sync_at=utc_now())
    runner = DiagnosticRunner(db_path=db_path)
    result = runner._check_poller_state()
    assert result.ok


# ------------------------------------------------------------------ #
# run_all                                                              #
# ------------------------------------------------------------------ #


def test_run_all_returns_results_for_healthy_db(runner: DiagnosticRunner):
    results = runner.run_all()
    assert len(results) >= 4
    assert all(isinstance(r, DiagnosticResult) for r in results)
    failed = [r for r in results if not r.ok]
    # Only Gmail credentials may fail in CI (no token, no client_secret.json).
    unexpected = [r for r in failed if r.name != "gmail_credentials"]
    assert not unexpected, [str(r) for r in unexpected]
    assert {r.name for r in results} >= {
        "db_file",
        "db_connectivity",
        "schema_revision",
        "schema_tables",
        "enum_values",
        "poller_state",
        "config_paths",
        "gmail_credentials",
    }


def test_run_all_catches_exception_from_bad_check(tmp_path: Path):
    """A check that raises must not abort the whole suite — it must be caught."""
    runner = DiagnosticRunner(db_path=tmp_path / "nonexistent.db")
    results = runner.run_all()
    # Some checks will fail gracefully but none should propagate exceptions.
    assert all(isinstance(r, DiagnosticResult) for r in results)


# ------------------------------------------------------------------ #
# Complete schema + revision                                           #
# ------------------------------------------------------------------ #


def test_required_tables_include_every_model_table():
    from backend.diagnostics import required_tables

    assert required_tables() == {
        "application",
        "statushistory",
        "applicationevent",
        "applicationthreadid",
        "suppressrule",
        "pollerstate",
        "processedmessage",
        "prospect",
        "evidence",
        "mergeoperation",
        "duplicatedismissal",
        "collector",
        "collectorenrollment",
        "collectionsource",
        "collectionrun",
        "collectionbatch",
        "sourceitem",
        "sourceobservation",
    }


@pytest.mark.parametrize("table", ["applicationevent", "applicationthreadid", "prospect"])
def test_check_schema_tables_reports_each_missing_table(db: DataStore, db_path: Path, table):
    db.close()
    conn = sqlite3.connect(db_path)  # test-only fixture surgery on a throwaway DB
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()
    result = DiagnosticRunner(db_path=db_path)._check_schema_tables()
    assert not result.ok
    assert table in result.detail


def test_check_schema_tables_requires_alembic_version(db: DataStore, db_path: Path):
    db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE alembic_version")
    conn.commit()
    conn.close()
    result = DiagnosticRunner(db_path=db_path)._check_schema_tables()
    assert not result.ok
    assert "alembic_version" in result.detail


def test_check_schema_revision_current(runner: DiagnosticRunner):
    from backend.db.schema import head_revision

    result = runner._check_schema_revision()
    assert result.ok
    assert head_revision() in result.detail


def test_check_schema_revision_unversioned_database(tmp_path: Path):
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE application (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    result = DiagnosticRunner(db_path=legacy)._check_schema_revision()
    assert not result.ok
    assert "unversioned" in result.detail
    assert "migrate_database.py" in result.detail


def test_diagnostics_do_not_migrate_an_outdated_database(tmp_path: Path):
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE application (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    DiagnosticRunner(db_path=legacy).run_all()
    tables = DataStore.inspect_schema_tables(legacy)
    assert "alembic_version" not in tables
    assert tables == {"application"}


# ------------------------------------------------------------------ #
# Gmail credentials                                                    #
# ------------------------------------------------------------------ #

_GOOD_TOKEN = '{"refresh_token": "r", "client_id": "c", "client_secret": "s", "token": "t"}'


@pytest.fixture
def poller_on(monkeypatch):
    """Credential checks only run when polling is enabled (tests default to disabled)."""
    monkeypatch.setattr("backend.config.POLLER_ENABLED", True)


@pytest.fixture
def no_keychain(monkeypatch, poller_on):
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None)


def test_gmail_credentials_not_loaded_when_polling_disabled(runner: DiagnosticRunner, monkeypatch):
    from unittest.mock import MagicMock

    get_password = MagicMock()
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setenv("GMAIL_TOKEN_JSON", _GOOD_TOKEN)
    result = runner._check_gmail_credentials()
    assert result.ok
    assert "POLLER_ENABLED=false" in result.detail
    get_password.assert_not_called()


def test_gmail_env_token_passes_without_client_secret_file(
    runner: DiagnosticRunner, monkeypatch, tmp_path: Path, no_keychain
):
    monkeypatch.setenv("GMAIL_TOKEN_JSON", _GOOD_TOKEN)
    monkeypatch.setattr("backend.poller.gmail_poller.CREDENTIALS_PATH", tmp_path / "absent.json")
    result = runner._check_gmail_credentials()
    assert result.ok, result.detail
    assert "GMAIL_TOKEN_JSON" in result.detail
    assert "web re-auth unavailable" in result.detail
    for secret in ('"r"', '"s"', '"t"'):
        assert secret not in result.detail


def test_gmail_env_token_missing_refresh_fields_fails(
    runner: DiagnosticRunner, monkeypatch, no_keychain
):
    monkeypatch.setenv("GMAIL_TOKEN_JSON", '{"token": "only-access-token"}')
    result = runner._check_gmail_credentials()
    assert not result.ok
    assert "refresh_token" in result.detail
    assert "only-access-token" not in result.detail


def test_gmail_env_token_invalid_json_fails(runner: DiagnosticRunner, monkeypatch, no_keychain):
    monkeypatch.setenv("GMAIL_TOKEN_JSON", "not-json")
    result = runner._check_gmail_credentials()
    assert not result.ok
    assert "not valid JSON" in result.detail


@pytest.mark.usefixtures("poller_on")
def test_gmail_keychain_token_passes(runner: DiagnosticRunner, monkeypatch, tmp_path: Path):
    monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: _GOOD_TOKEN)
    monkeypatch.setattr("backend.poller.gmail_poller.CREDENTIALS_PATH", tmp_path / "absent.json")
    result = runner._check_gmail_credentials()
    assert result.ok
    assert "keychain" in result.detail


@pytest.mark.usefixtures("poller_on")
def test_gmail_keychain_unavailable_is_handled(
    runner: DiagnosticRunner, monkeypatch, tmp_path: Path
):
    import keyring.errors

    def boom(*_a, **_k):
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
    monkeypatch.setattr("keyring.get_password", boom)
    monkeypatch.setattr("backend.poller.gmail_poller.CREDENTIALS_PATH", tmp_path / "absent.json")
    result = runner._check_gmail_credentials()
    assert not result.ok
    assert "No Gmail credentials" in result.detail


def test_gmail_client_secret_without_token_asks_for_setup(
    runner: DiagnosticRunner, monkeypatch, tmp_path: Path, no_keychain
):
    secret_file = tmp_path / "client_secret.json"
    secret_file.write_text("{}")
    monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
    monkeypatch.setattr("backend.poller.gmail_poller.CREDENTIALS_PATH", secret_file)
    result = runner._check_gmail_credentials()
    assert not result.ok
    assert "setup_wizard.py" in result.detail


def test_config_paths_no_longer_requires_client_secret(
    runner: DiagnosticRunner, monkeypatch, tmp_path
):
    monkeypatch.setattr("backend.config.CREDENTIALS_PATH", tmp_path / "absent.json")
    assert runner._check_config_paths().ok


def test_failing_check_name_is_not_mangled(runner: DiagnosticRunner, monkeypatch):
    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_check_config_paths", explode)
    explode.__name__ = "_check_config_paths"
    results = runner.run_all()
    assert any(r.name == "config_paths" and not r.ok for r in results)
