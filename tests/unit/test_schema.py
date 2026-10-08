"""Tests for backend/db/schema.py and the Alembic revisions under backend/db/alembic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

from backend.db import schema
from backend.db.backup import list_backups, load_manifest
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import ApplicationStatus
from backend.db.schema import SchemaOutdatedError, SchemaPolicy, SchemaState
from tests.conftest import build_legacy_database, seed_legacy_rows


def _engine(path: Path):
    return create_engine(f"sqlite:///{path}")


def _ddl(path: Path) -> dict[str, str]:
    """Normalised sqlite_master contents (object name -> SQL), excluding alembic_version."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ).fetchall()
    finally:
        conn.close()
    return {name: " ".join(sql.split()) for name, sql in rows}


# ------------------------------------------------------------------ #
# Baseline correctness                                                 #
# ------------------------------------------------------------------ #


def test_single_baseline_head() -> None:
    assert schema.head_revision() == "0001_baseline"
    assert schema.known_revisions() == {"0001_baseline"}


def test_fresh_database_matches_pre_phase1_schema_exactly(tmp_path: Path) -> None:
    """A database created by the baseline is byte-for-byte the schema origin/main created."""
    fresh = tmp_path / "fresh.db"
    DataStore(fresh).close()
    legacy = build_legacy_database(tmp_path / "legacy.db")
    assert _ddl(fresh) == _ddl(legacy)


def test_models_and_migrations_agree(tmp_path: Path) -> None:
    """Autogenerate finds nothing to do: models.py and the revisions describe one schema."""
    path = tmp_path / "head.db"
    DataStore(path).close()
    engine = _engine(path)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []


# ------------------------------------------------------------------ #
# Status detection                                                     #
# ------------------------------------------------------------------ #


def test_status_states(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert schema.read_status(empty).state is SchemaState.EMPTY

    legacy = build_legacy_database(tmp_path / "legacy.db")
    status = schema.read_status(legacy)
    assert status.state is SchemaState.UNVERSIONED
    assert status.needs_upgrade
    assert status.current_revision is None

    current = tmp_path / "current.db"
    DataStore(current).close()
    status = schema.read_status(current)
    assert status.is_current
    assert status.current_revision == "0001_baseline"


def test_unknown_revision_is_detected_and_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    DataStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE alembic_version SET version_num = '9999_from_the_future'")
    conn.commit()
    conn.close()
    assert schema.read_status(path).state is SchemaState.UNKNOWN
    with pytest.raises(SchemaOutdatedError):
        DataStore(path, schema_policy=SchemaPolicy.AUTO)


def test_read_status_does_not_modify_the_file(tmp_path: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    before = legacy.read_bytes()
    schema.read_status(legacy)
    assert legacy.read_bytes() == before


# ------------------------------------------------------------------ #
# Upgrade from the pre-Phase-1 schema                                  #
# ------------------------------------------------------------------ #


def test_upgrade_pre_phase1_database_preserves_data(tmp_path: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    counts = seed_legacy_rows(legacy)
    before_ddl = _ddl(legacy)

    store = DataStore(legacy, schema_policy=SchemaPolicy.INSPECT)
    status = store.upgrade_schema()
    store.close()

    assert status.is_current
    assert _ddl(legacy) == before_ddl  # adoption: no schema object changed
    assert DataStore.count_rows_readonly(legacy) == {**counts, "processedmessage": 1}

    store = DataStore(legacy, schema_policy=SchemaPolicy.VERIFY)
    items, total = store.get_applications(ApplicationFilter())
    assert total == 3
    globex = next(a for a in items if a.company == "Globex")
    assert globex.source_portal == "Instahyre"  # legacy data fix carried by the baseline
    assert globex.current_status is ApplicationStatus.REJECTED
    assert store.find_application_by_thread_id("t-3").company == "Globex"
    assert len(store.get_prospects()) == 1
    store.close()


def test_upgrade_older_pre_alter_database(tmp_path: Path) -> None:
    """Databases that predate the startup ALTERs and newer tables are completed exactly as
    create_all + _migrate_schema used to."""
    legacy = build_legacy_database(tmp_path / "old.db", "legacy_pre_alter_schema.sql")
    seed_legacy_rows(legacy, with_phase1_tables=False)

    store = DataStore(legacy, schema_policy=SchemaPolicy.INSPECT)
    assert store.upgrade_schema().is_current
    store.close()

    engine = _engine(legacy)
    insp = inspect(engine)
    assert {"applicationevent", "applicationthreadid"} <= set(insp.get_table_names())
    app_cols = {c["name"]: c for c in insp.get_columns("application")}
    assert "withdraw_reason" in app_cols
    assert app_cols["application_method"]["nullable"] is False
    assert "application_id" in {c["name"] for c in insp.get_columns("prospect")}
    assert "ix_prospect_application_id" in {ix["name"] for ix in insp.get_indexes("prospect")}

    store = DataStore(legacy, schema_policy=SchemaPolicy.VERIFY)
    items, _ = store.get_applications(ApplicationFilter())
    assert {a.application_method for a in items} == {"Unknown"}
    # Runtime maintenance rebuilt the thread index and milestone events.
    assert store.find_application_by_thread_id("t-2").company == "Globex"
    assert store.get_application_events(2)
    store.close()


def test_repeated_migration_is_a_no_op(tmp_path: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    seed_legacy_rows(legacy)
    store = DataStore(legacy, schema_policy=SchemaPolicy.INSPECT)
    store.upgrade_schema()
    snapshot = (_ddl(legacy), DataStore.count_rows_readonly(legacy))
    for _ in range(3):
        assert store.upgrade_schema().is_current
    store.close()
    assert (_ddl(legacy), DataStore.count_rows_readonly(legacy)) == snapshot


def test_baseline_rerun_on_existing_schema_is_idempotent(tmp_path: Path) -> None:
    """Even if the version row is lost, re-running the baseline changes nothing."""
    path = tmp_path / "db.db"
    DataStore(path).close()
    before = _ddl(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE alembic_version")
    conn.commit()
    conn.close()
    store = DataStore(path, schema_policy=SchemaPolicy.INSPECT)
    assert store.upgrade_schema().is_current
    store.close()
    assert _ddl(path) == before


def test_baseline_cannot_be_downgraded(tmp_path: Path) -> None:
    from alembic import command

    path = tmp_path / "db.db"
    DataStore(path).close()
    cfg = schema.alembic_config()
    engine = _engine(path)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="Restore a pre-migration backup"):
            command.downgrade(cfg, "base")
    assert schema.read_status(path).is_current


# ------------------------------------------------------------------ #
# Startup policies                                                     #
# ------------------------------------------------------------------ #


def test_verify_policy_refuses_outdated_database(tmp_path: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    with pytest.raises(SchemaOutdatedError) as err:
        DataStore(legacy, schema_policy=SchemaPolicy.VERIFY)
    assert err.value.status.state is SchemaState.UNVERSIONED
    assert "migrate_database.py" in str(err.value)
    assert schema.read_status(legacy).state is SchemaState.UNVERSIONED  # untouched


def test_verify_policy_still_creates_an_empty_database(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "new.db", schema_policy=SchemaPolicy.VERIFY)
    assert store.schema_status.is_current
    store.close()


def test_auto_policy_backs_up_before_migrating(tmp_path: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    seed_legacy_rows(legacy)
    store = DataStore(legacy, schema_policy=SchemaPolicy.AUTO)
    assert store.schema_status.is_current
    store.close()

    backups = list_backups(tmp_path / "backups")
    assert len(backups) == 1
    manifest = load_manifest(backups[0])
    assert manifest.label == "auto-pre-migration"
    assert manifest.schema_revision is None  # captured before the migration
    assert manifest.application_count == 3


def test_inspect_policy_never_changes_schema(tmp_path: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    store = DataStore(legacy, schema_policy=SchemaPolicy.INSPECT)
    assert store.schema_status.state is SchemaState.UNVERSIONED
    store.close()
    assert "alembic_version" not in DataStore.inspect_schema_tables(legacy)


def test_default_policy(monkeypatch) -> None:
    monkeypatch.setattr("backend.config.APP_ENV", "production")
    monkeypatch.setattr("backend.config.DB_AUTO_MIGRATE", True)
    assert schema.default_policy() is SchemaPolicy.VERIFY  # never automatic in production
    monkeypatch.setattr("backend.config.APP_ENV", "development")
    assert schema.default_policy() is SchemaPolicy.AUTO
    monkeypatch.setattr("backend.config.DB_AUTO_MIGRATE", False)
    assert schema.default_policy() is SchemaPolicy.VERIFY
