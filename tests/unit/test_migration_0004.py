"""Tests for Alembic revision 0004_merge_operations (from a Prompt 5 / 0003 database)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from backend.db import schema
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.schema import SchemaPolicy
from tests.conftest import build_legacy_database, seed_legacy_rows

PRE_0004 = {
    "application": (
        "id, company, role, source_portal, application_method, job_url, applied_date, "
        "current_status, thread_ids, is_false_positive, withdraw_reason, created_at, updated_at, "
        "normalized_company, normalized_role, canonical_job_url, external_job_id, last_evidence_at"
    ),
    "statushistory": (
        'id, application_id, from_status, to_status, "trigger", changed_at, message_id'
    ),
    "applicationevent": (
        "id, application_id, event_type, occurred_at, interview_round, source, source_message_id, "
        "status_history_id, notes, created_at"
    ),
    "evidence": "*",
    "applicationthreadid": "*",
    "prospect": "*",
}


def _sql(path: Path, query: str, *params) -> list[tuple]:
    conn = sqlite3.connect(path)  # test-only raw access
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _engine(path: Path):
    return create_engine(f"sqlite:///{path}")


def _db_at_0003(tmp_path: Path) -> Path:
    path = build_legacy_database(tmp_path / "p5.db", "phase1_schema.sql")
    seed_legacy_rows(path)
    schema.upgrade(_engine(path), "0003_resolver_audit")
    return path


def _snapshot(path: Path) -> dict:
    return {t: _sql(path, f"SELECT {cols} FROM {t} ORDER BY 1") for t, cols in PRE_0004.items()}


def test_upgrade_is_additive_and_everything_starts_active(tmp_path: Path) -> None:
    path = _db_at_0003(tmp_path)
    before = _snapshot(path)
    total = _sql(path, "SELECT COUNT(*) FROM application")[0][0]
    schema.upgrade(_engine(path))
    assert schema.read_status(path).is_current
    assert _snapshot(path) == before
    assert _sql(path, "SELECT DISTINCT record_state FROM application") == [("active",)]
    assert _sql(
        path, "SELECT COUNT(*) FROM statushistory WHERE superseded_by_merge_id IS NOT NULL"
    ) == [(0,)]
    store = DataStore(path, schema_policy=SchemaPolicy.VERIFY)
    assert store.get_applications(ApplicationFilter())[1] == total
    store.close()


def test_older_release_inserts_default_to_active(tmp_path: Path) -> None:
    path = _db_at_0003(tmp_path)
    schema.upgrade(_engine(path))
    _sql(
        path,
        "INSERT INTO application (company, role, source_portal, application_method, applied_date, "
        "current_status, thread_ids, is_false_positive, created_at, updated_at) VALUES "
        "('Old Release', 'SRE', 'LinkedIn', 'Unknown', '2026-06-01', 'Applied', '[]', 0, "
        "'2026-06-01', '2026-06-01')",
    )
    assert _sql(path, "SELECT record_state FROM application WHERE company='Old Release'") == [
        ("active",)
    ]


def test_upgrade_is_idempotent_after_rollback_stamp(tmp_path: Path) -> None:
    path = _db_at_0003(tmp_path)
    schema.upgrade(_engine(path))
    ddl = sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
    schema.stamp(_engine(path), "0003_resolver_audit")
    schema.upgrade(_engine(path))
    assert sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")) == ddl


def test_downgrade_refuses_while_merges_are_in_effect(tmp_path: Path) -> None:
    from alembic import command

    path = _db_at_0003(tmp_path)
    schema.upgrade(_engine(path))
    _sql(
        path,
        "UPDATE application SET record_state='merged', merged_into_application_id=1 WHERE id=2",
    )
    cfg = schema.alembic_config()
    with pytest.raises(RuntimeError, match="undo those merges"):
        with _engine(path).begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0003_resolver_audit")
    _sql(path, "UPDATE application SET record_state='active', merged_into_application_id=NULL")
    before = _snapshot(path)
    with _engine(path).begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0003_resolver_audit")
    assert "mergeoperation" not in {r[0] for r in _sql(path, "SELECT name FROM sqlite_master")}
    assert _snapshot(path) == before
