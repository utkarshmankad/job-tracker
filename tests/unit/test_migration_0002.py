"""Tests for Alembic revision 0002_evidence_model, starting from a Phase 1 database."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from starlette.testclient import TestClient

from backend.db import schema
from backend.db.data_store import DataStore
from backend.db.schema import SchemaPolicy
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.normalization import evidence_fingerprint
from backend.engine.status_updater import StatusUpdater
from tests.conftest import build_legacy_database, seed_legacy_rows

PHASE1 = "phase1_schema.sql"
LEGACY_TABLES = (
    "statushistory",
    "applicationevent",
    "applicationthreadid",
    "prospect",
    "processedmessage",
    "suppressrule",
    "pollerstate",
)
APPLICATION_PHASE1_COLUMNS = (
    "id, company, role, source_portal, application_method, job_url, applied_date, "
    "current_status, thread_ids, is_false_positive, withdraw_reason, created_at, updated_at"
)


def _sql(path: Path, query: str, *params) -> list[tuple]:
    conn = sqlite3.connect(path)  # test-only raw reads of the migrated file
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _legacy_snapshot(path: Path) -> dict[str, list[tuple]]:
    snapshot = {t: _sql(path, f"SELECT * FROM {t} ORDER BY 1") for t in LEGACY_TABLES}
    snapshot["application"] = _sql(
        path, f"SELECT {APPLICATION_PHASE1_COLUMNS} FROM application ORDER BY id"
    )
    return snapshot


def _phase1_db(tmp_path: Path, name: str = "phase1.db") -> Path:
    path = build_legacy_database(tmp_path / name, PHASE1)
    seed_legacy_rows(path)  # 3 apps, history, event, thread ids, 1 linked prospect
    _sql(
        path,
        "INSERT INTO prospect (source_portal, category, title, sender, snippet, received_at, "
        "gmail_message_id, gmail_thread_id, application_id, status, classification_reason, "
        "created_at, updated_at) VALUES ('LinkedIn', 'meeting', 'LinkedIn recruiting activity', "
        "'LinkedIn <x@linkedin.com>', '', '2026-05-06 08:30:00.000000', 'p-2', 'pt-2', NULL, "
        "'New', 'meeting', '2026-05-06 09:00:00.000000', '2026-05-06 09:00:00.000000')",
    )
    return path


def _upgrade(path: Path, revision: str = "head") -> None:
    schema.upgrade(create_engine(f"sqlite:///{path}"), revision)


def _stamp(path: Path, revision: str) -> None:
    schema.stamp(create_engine(f"sqlite:///{path}"), revision)


def test_phase1_fixture_is_at_baseline(tmp_path: Path) -> None:
    status = schema.read_status(_phase1_db(tmp_path))
    assert status.current_revision == "0001_baseline"
    assert status.needs_upgrade


def test_upgrade_preserves_every_existing_row(tmp_path: Path) -> None:
    path = _phase1_db(tmp_path)
    before = _legacy_snapshot(path)
    _upgrade(path)
    assert schema.read_status(path).is_current
    assert _legacy_snapshot(path) == before


def test_prospects_are_backfilled_as_evidence_without_invented_facts(tmp_path: Path) -> None:
    path = _phase1_db(tmp_path)
    _upgrade(path)
    store = DataStore(path, schema_policy=SchemaPolicy.VERIFY)

    linked = store.get_evidence_by_external_id("gmail", "p-1")
    assert (linked.evidence_type, linked.source, linked.thread_id) == ("email", "gmail", "pt-1")
    assert (linked.sender, linked.subject, linked.snippet) == (
        "Recruiter",
        "Role at Acme",
        None,
    )
    assert linked.normalized_subject == "role at acme"
    assert linked.occurred_at.isoformat().startswith("2026-05-04T10:00:00")
    assert (linked.application_id, linked.link_method, linked.link_confidence) == (
        1,
        "backfill",
        1.0,
    )
    assert linked.processing_status == "informational"
    assert linked.raw_metadata["backfill"] == {
        "origin": "prospect",
        "prospect_id": 1,
        "revision": "0002_evidence_model",
    }
    assert linked.content_fingerprint == evidence_fingerprint(
        evidence_type="email", source="gmail", external_id="p-1"
    )

    fallback = store.get_evidence_by_external_id("gmail", "p-2")
    assert fallback.subject is None  # the parser's placeholder title is not a real subject
    assert fallback.normalized_subject is None
    assert fallback.application_id is None and fallback.link_method is None

    # Nothing else was turned into evidence: processed messages, status-history message IDs
    # and thread IDs carry no message date, so they are deliberately not backfilled.
    assert store.count_evidence()["total"] == 2
    assert _sql(path, "SELECT COUNT(*) FROM processedmessage")[0][0] == 1
    store.close()


def test_last_evidence_at_and_identity_columns_after_upgrade(tmp_path: Path) -> None:
    path = _phase1_db(tmp_path)
    _upgrade(path)
    rows = _sql(
        path, "SELECT id, last_evidence_at, normalized_company FROM application ORDER BY id"
    )
    assert rows[0][1].startswith("2026-05-04 10:00:00")  # from the linked prospect
    assert rows[1][1] is None and rows[2][1] is None
    assert all(r[2] is None for r in rows)  # derived columns are runtime-owned...

    store = DataStore(path, schema_policy=SchemaPolicy.VERIFY)  # ...and filled on open
    store.close()
    assert [r[0] for r in _sql(path, "SELECT normalized_company FROM application ORDER BY id")] == [
        "acme",
        "globex",
        "initech",
    ]


def test_repeated_upgrade_after_rollback_stamp_is_idempotent(tmp_path: Path) -> None:
    path = _phase1_db(tmp_path)
    _upgrade(path)
    schema_sql = sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
    evidence_rows = _sql(path, "SELECT * FROM evidence ORDER BY id")
    for _ in range(2):
        _stamp(path, "0001_baseline")  # documented code-only rollback
        _upgrade(path)  # roll forward again
    assert schema.read_status(path).is_current
    assert sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")) == (
        schema_sql
    )
    assert _sql(path, "SELECT * FROM evidence ORDER BY id") == evidence_rows


def test_phase1_style_writes_work_on_the_expanded_schema(tmp_path: Path) -> None:
    """The old release only knows Phase 1 columns; its inserts must still succeed, and the
    new release repairs the derived columns when it starts again."""
    path = _phase1_db(tmp_path)
    _upgrade(path)
    _stamp(path, "0001_baseline")
    _sql(
        path,
        f"INSERT INTO application ({APPLICATION_PHASE1_COLUMNS}) VALUES (NULL, 'Hooli Inc', "
        "'SRE', 'LinkedIn', 'Unknown', NULL, '2026-06-01 00:00:00', 'Applied', '[]', 0, NULL, "
        "'2026-06-01 00:00:00', '2026-06-01 00:00:00')",
    )
    _upgrade(path)
    DataStore(path, schema_policy=SchemaPolicy.VERIFY).close()
    assert _sql(path, "SELECT normalized_company FROM application WHERE company='Hooli Inc'") == [
        ("hooli",)
    ]


def test_downgrade_removes_only_phase2_objects(tmp_path: Path) -> None:
    from alembic import command

    path = _phase1_db(tmp_path)
    before = _legacy_snapshot(path)
    _upgrade(path)
    cfg = schema.alembic_config()
    with create_engine(f"sqlite:///{path}").begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0001_baseline")
    tables = {r[0] for r in _sql(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "evidence" not in tables
    columns = {r[1] for r in _sql(path, "PRAGMA table_info(application)")}
    assert "normalized_company" not in columns
    assert _legacy_snapshot(path) == before
    assert DataStore.integrity_check(path) == ["ok"]
    _upgrade(path)
    assert schema.read_status(path).is_current


def test_analytics_unaffected_by_migration(tmp_path: Path, test_auth_user) -> None:
    """Analytics read only pre-existing data: the migrated database gives the same answers
    as the same database with every Phase 2 addition stripped out."""
    from backend.main import app

    migrated = _phase1_db(tmp_path)
    _upgrade(migrated)
    stripped = tmp_path / "stripped.db"
    shutil.copy(migrated, stripped)
    _sql(stripped, "DELETE FROM evidence")
    _sql(
        stripped,
        "UPDATE application SET normalized_company=NULL, normalized_role=NULL, "
        "canonical_job_url=NULL, external_job_id=NULL, last_evidence_at=NULL",
    )

    def without_timestamps(value):
        if isinstance(value, dict):
            return {k: without_timestamps(v) for k, v in value.items() if k != "generated_at"}
        if isinstance(value, list):
            return [without_timestamps(v) for v in value]
        return value

    def analytics(path: Path) -> dict:
        db = DataStore(path, schema_policy=SchemaPolicy.VERIFY)
        with TestClient(app) as client:
            app.state.db = db
            app.state.updater = StatusUpdater(db, DuplicateDetector(db))
            out = {
                endpoint: without_timestamps(client.get(f"/api/v1/{endpoint}").json())
                for endpoint in (
                    "insights",
                    "insights/flow",
                    "insights/pulse",
                    "insights/rejection",
                    "insights/conversions",
                )
            }
        db.close()
        return out

    assert analytics(migrated) == analytics(stripped)


@pytest.mark.parametrize("revision", ["0001_baseline"])
def test_stamp_cli_only_moves_down(tmp_path: Path, revision: str) -> None:
    from click.testing import CliRunner

    from backend.db.recovery_cli import migrate_group

    path = _phase1_db(tmp_path)
    _upgrade(path)
    runner = CliRunner()
    up = runner.invoke(
        migrate_group,
        ["stamp", "0002_evidence_model", "--db", str(path), "--backup-dir", str(tmp_path / "b")],
    )
    assert up.exit_code != 0 and "not older" in up.output
    down = runner.invoke(
        migrate_group,
        ["stamp", revision, "--db", str(path), "--backup-dir", str(tmp_path / "b")],
    )
    assert down.exit_code == 0, down.output
    assert "Verified:" in down.output
    assert schema.read_status(path).current_revision == revision
    assert "evidence" in {r[0] for r in _sql(path, "SELECT name FROM sqlite_master")}
