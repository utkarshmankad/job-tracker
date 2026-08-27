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


def test_check_db_connectivity_missing_db(tmp_path: Path):
    # A valid SQLite path that doesn't exist yet — DataStore will CREATE it,
    # so connectivity should succeed.
    runner = DiagnosticRunner(db_path=tmp_path / "new.db")
    result = runner._check_db_connectivity()
    assert result.ok


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
    # Only config path checks may fail in CI (no credentials.json).
    non_config_failures = [r for r in failed if r.name != "config_paths"]
    assert not non_config_failures, [str(r) for r in non_config_failures]


def test_run_all_catches_exception_from_bad_check(tmp_path: Path):
    """A check that raises must not abort the whole suite — it must be caught."""
    runner = DiagnosticRunner(db_path=tmp_path / "nonexistent.db")
    results = runner.run_all()
    # Some checks will fail gracefully but none should propagate exceptions.
    assert all(isinstance(r, DiagnosticResult) for r in results)
