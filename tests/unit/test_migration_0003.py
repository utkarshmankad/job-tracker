"""Tests for Alembic revision 0003_resolver_audit."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine

from backend.db import schema
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.schema import SchemaPolicy
from tests.conftest import build_legacy_database, seed_legacy_rows

APP_COLUMNS_BEFORE_0003 = (
    "id, company, role, source_portal, application_method, job_url, applied_date, "
    "current_status, thread_ids, is_false_positive, withdraw_reason, created_at, updated_at, "
    "normalized_company, normalized_role, canonical_job_url, external_job_id, last_evidence_at"
)
EVIDENCE_COLUMNS_BEFORE_0003 = (
    "id, evidence_type, source, external_id, thread_id, sender, subject, snippet, occurred_at, "
    "processing_status, application_id, link_method, link_confidence"
)


def _sql(path: Path, query: str, *params) -> list[tuple]:
    conn = sqlite3.connect(path)  # test-only raw access to the migrated file
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _engine(path: Path):
    return create_engine(f"sqlite:///{path}")


def _db_at_0002(tmp_path: Path) -> Path:
    path = build_legacy_database(tmp_path / "p2.db", "phase1_schema.sql")
    seed_legacy_rows(path)
    schema.upgrade(_engine(path), "0002_evidence_model")
    insert = (
        "INSERT INTO evidence (evidence_type, source, external_id, sender, occurred_at, "
        "captured_at, raw_metadata, content_fingerprint, processing_status, application_id, "
        "link_method, link_confidence, created_at, updated_at) VALUES ('email', 'gmail', ?, ?, "
        "'2026-05-01 00:00:00', '2026-05-01 00:00:00', ?, ?, ?, ?, ?, ?, "
        "'2026-05-02 00:00:00', '2026-05-03 00:00:00')"
    )
    rows = [
        ("m-manual", "Jane <jane@acme.com>", "{}", "fp1", "linked", 1, "manual", 1.0),
        (
            "m-thread",
            "Naukri <noreply@naukri.com>",
            '{"parser": {"job_url": "https://www.linkedin.com/jobs/view/3912345678/"}}',
            "fp2",
            "linked",
            2,
            "thread",
            1.0,
        ),
        ("m-review", None, "{}", "fp3", "needs_review", None, None, None),
        ("m-pending", None, "{}", "fp4", "pending", None, None, None),
    ]
    for row in rows:
        _sql(path, insert, *row)
    return path


def _snapshot(path: Path) -> dict:
    return {
        "application": _sql(path, f"SELECT {APP_COLUMNS_BEFORE_0003} FROM application ORDER BY id"),
        "evidence": _sql(path, f"SELECT {EVIDENCE_COLUMNS_BEFORE_0003} FROM evidence ORDER BY id"),
        **{
            t: _sql(path, f"SELECT * FROM {t} ORDER BY 1")
            for t in ("statushistory", "applicationevent", "applicationthreadid", "prospect")
        },
    }


def test_upgrade_records_decision_owners_and_changes_nothing_else(tmp_path: Path) -> None:
    path = _db_at_0002(tmp_path)
    before = _snapshot(path)
    total_before = _sql(path, "SELECT COUNT(*) FROM application")[0][0]
    schema.upgrade(_engine(path))
    assert schema.read_status(path).current_revision == "0003_resolver_audit"
    assert _snapshot(path) == before
    assert _sql(path, "SELECT COUNT(*) FROM application")[0][0] == total_before

    owners = dict(
        (row[0], row[1:])
        for row in _sql(
            path,
            "SELECT external_id, decided_by, resolver_version, resolver_decision, decided_at "
            "FROM evidence",
        )
    )
    assert owners["m-manual"][:3] == ("human", None, None)
    assert owners["m-thread"][:3] == ("resolver", "1", "linked")
    assert owners["m-review"][:3] == ("resolver", "1", "review_required")
    assert owners["m-pending"] == (None, None, None, None)
    assert owners["m-manual"][3].startswith("2026-05-03")  # decided_at = last update


def test_derived_evidence_signals_are_filled_at_runtime(tmp_path: Path) -> None:
    path = _db_at_0002(tmp_path)
    schema.upgrade(_engine(path))
    DataStore(path, schema_policy=SchemaPolicy.VERIFY).close()
    rows = dict(
        (row[0], row[1:])
        for row in _sql(
            path,
            "SELECT external_id, sender_address, sender_domain, canonical_job_url, "
            "external_job_id FROM evidence",
        )
    )
    assert rows["m-manual"][:2] == ("jane@acme.com", "acme.com")
    assert rows["m-thread"] == (
        "noreply@naukri.com",
        "naukri.com",
        "https://linkedin.com/jobs/view/3912345678",
        "3912345678",
    )


def test_upgrade_is_idempotent_after_rollback_stamp(tmp_path: Path) -> None:
    path = _db_at_0002(tmp_path)
    schema.upgrade(_engine(path))
    ddl = sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
    evidence = _sql(path, "SELECT * FROM evidence ORDER BY id")
    schema.stamp(_engine(path), "0002_evidence_model")
    schema.upgrade(_engine(path))
    assert sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")) == ddl
    assert _sql(path, "SELECT * FROM evidence ORDER BY id") == evidence


def test_downgrade_to_0002_and_back(tmp_path: Path) -> None:
    from alembic import command

    path = _db_at_0002(tmp_path)
    before = _snapshot(path)
    schema.upgrade(_engine(path))
    cfg = schema.alembic_config()
    with _engine(path).begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0002_evidence_model")
    columns = {r[1] for r in _sql(path, "PRAGMA table_info(evidence)")}
    assert "resolver_result" not in columns
    assert _snapshot(path) == before
    schema.upgrade(_engine(path))
    assert schema.read_status(path).is_current


def test_application_totals_unchanged_through_full_chain(tmp_path: Path) -> None:
    path = build_legacy_database(tmp_path / "chain.db", "phase1_schema.sql")
    seed_legacy_rows(path)
    before = _sql(path, "SELECT COUNT(*) FROM application")[0][0]
    store = DataStore(path, schema_policy=SchemaPolicy.INSPECT)
    store.upgrade_schema()
    store.close()
    store = DataStore(path, schema_policy=SchemaPolicy.VERIFY)
    assert store.get_applications(ApplicationFilter())[1] == before
    store.close()
