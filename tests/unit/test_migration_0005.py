"""Tests for Alembic revision 0005_source_collection (from a 0004 database)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from backend.db import schema
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.schema import SchemaPolicy
from tests.conftest import build_legacy_database, seed_legacy_rows

NEW_TABLES = {
    "collector",
    "collectorenrollment",
    "collectionsource",
    "collectionrun",
    "collectionbatch",
    "sourceitem",
    "sourceobservation",
}


def _sql(path: Path, query: str) -> list[tuple]:
    conn = sqlite3.connect(path)  # test-only raw access
    try:
        rows = conn.execute(query).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _engine(path: Path):
    return create_engine(f"sqlite:///{path}")


def _db_at_0004(tmp_path: Path) -> Path:
    path = build_legacy_database(tmp_path / "p6.db", "phase1_schema.sql")
    seed_legacy_rows(path)
    schema.upgrade(_engine(path), "0004_merge_operations")
    return path


def _tables(path: Path) -> set[str]:
    return {r[0] for r in _sql(path, "SELECT name FROM sqlite_master WHERE type='table'")}


def _existing_data(path: Path) -> dict[str, list[tuple]]:
    """Every row of every pre-0005 table (columns unchanged by this revision)."""
    return {
        t: _sql(path, f'SELECT * FROM "{t}" ORDER BY 1')
        for t in sorted(_tables(path) - {"alembic_version"})
    }


def test_upgrade_is_additive(tmp_path: Path) -> None:
    path = _db_at_0004(tmp_path)
    before = _existing_data(path)
    total = _sql(path, "SELECT COUNT(*) FROM application")[0][0]
    schema.upgrade(_engine(path))
    assert schema.read_status(path).current_revision == "0005_source_collection"
    assert NEW_TABLES <= _tables(path)
    assert {t: rows for t, rows in _existing_data(path).items() if t in before} == before
    assert _sql(path, "PRAGMA foreign_key_check") == []
    store = DataStore(path, schema_policy=SchemaPolicy.VERIFY)
    assert store.get_applications(ApplicationFilter())[1] == total
    store.close()


def test_upgrade_is_idempotent_after_rollback_stamp(tmp_path: Path) -> None:
    path = _db_at_0004(tmp_path)
    schema.upgrade(_engine(path))
    ddl = sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
    schema.stamp(_engine(path), "0004_merge_operations")
    schema.upgrade(_engine(path))
    assert sorted(_sql(path, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")) == ddl


def _downgrade(path: Path, revision: str) -> None:
    from alembic import command

    cfg = schema.alembic_config()
    with _engine(path).begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def test_downgrade_refuses_while_observations_exist(tmp_path: Path) -> None:
    path = _db_at_0004(tmp_path)
    schema.upgrade(_engine(path))
    store = DataStore(path, schema_policy=SchemaPolicy.VERIFY)
    collector = store.create_collector(
        name="laptop", token_id="tid", scopes=["linkedin"], created_by=None
    )
    assert collector.id is not None
    run, _ = store.start_collection_run(
        run_key="run-1",
        collector_id=collector.id,
        source_key="linkedin",
        account_label="default",
        collector_version="1",
        adapter_version="1",
    )
    item, _ = store.upsert_source_item(
        {"source_key": "linkedin", "item_key": "id:1", "id_kind": "source_id", "company": "Co"}
    )
    assert item.id is not None and run.id is not None
    store.insert_source_observation(
        {
            "source_item_id": item.id,
            "run_id": run.id,
            "source_key": "linkedin",
            "contract_version": 1,
            "collector_version": "1",
            "adapter_version": "1",
            "extraction": "verified",
            "content_hash": "h",
            "fingerprint": "f",
            "observed_at": item.first_seen_at,
            "payload": {},
            "decision": "review",
        }
    )
    store.close()
    with pytest.raises(RuntimeError, match="source observations exist"):
        _downgrade(path, "0004_merge_operations")
    assert NEW_TABLES <= _tables(path)


def test_downgrade_drops_only_0005_objects(tmp_path: Path) -> None:
    path = _db_at_0004(tmp_path)
    before = _existing_data(path)
    schema.upgrade(_engine(path))
    _downgrade(path, "0004_merge_operations")
    assert not (NEW_TABLES & _tables(path))
    assert _existing_data(path) == before
    # Re-running the downgrade body after a partial failure must be safe.
    schema.upgrade(_engine(path))
    _downgrade(path, "0004_merge_operations")
    assert schema.read_status(path).current_revision == "0004_merge_operations"
