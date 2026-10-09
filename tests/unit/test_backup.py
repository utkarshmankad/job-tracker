"""Tests for backend/db/backup.py (and DataStore's backup primitives)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.db.backup import (
    BACKUP_TOOL_NAME,
    BACKUP_TOOL_VERSION,
    DATABASE_FILE_NAME,
    MANIFEST_NAME,
    BackupError,
    BackupValidationError,
    RestoreRefused,
    create_backup,
    latest_backup,
    list_backups,
    load_manifest,
    prune_backups,
    restore_backup,
    sha256_file,
    validate_backup,
    verify_backup,
)
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.db.schema import SchemaState, head_revision, read_status
from tests.conftest import build_legacy_database, seed_legacy_rows

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "applications.db"
    store = DataStore(path)
    for i in range(4):
        store.upsert_application(
            Application(
                company=f"Co{i}",
                role="Engineer",
                source_portal="LinkedIn",
                applied_date=utc_now(),
                current_status=ApplicationStatus.APPLIED,
                thread_ids=f'["thread-{i}"]',
            )
        )
    store.close()
    return path


@pytest.fixture
def backup_root(tmp_path: Path) -> Path:
    return tmp_path / "backups"


# ------------------------------------------------------------------ #
# Creation + manifest                                                  #
# ------------------------------------------------------------------ #


def test_create_backup_writes_copy_and_manifest(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, label="Pre Migration!", now=NOW)
    assert result.path.name == "20261009T120000Z-pre-migration"
    assert (result.path / DATABASE_FILE_NAME).is_file()
    raw = json.loads((result.path / MANIFEST_NAME).read_text())
    assert raw["created_at"] == NOW.isoformat()
    assert raw["schema_revision"] == head_revision()
    assert raw["sha256"] == sha256_file(result.database_path)
    assert raw["size_bytes"] == result.database_path.stat().st_size
    assert raw["application_count"] == 4
    assert raw["tool"] == BACKUP_TOOL_NAME
    assert raw["tool_version"] == BACKUP_TOOL_VERSION
    assert raw["integrity_check"] == "ok"
    assert raw["table_counts"]["applicationthreadid"] == 4


def test_backup_files_are_private(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    assert oct(result.path.stat().st_mode & 0o777) == "0o700"
    assert oct(result.database_path.stat().st_mode & 0o777) == "0o600"
    assert oct((result.path / MANIFEST_NAME).stat().st_mode & 0o777) == "0o600"


def test_backup_never_overwrites(live_db: Path, backup_root: Path) -> None:
    first = create_backup(live_db, backup_root, label="x", now=NOW)
    before = first.database_path.read_bytes()
    with pytest.raises(BackupError, match="refusing to overwrite"):
        create_backup(live_db, backup_root, label="x", now=NOW)
    assert first.database_path.read_bytes() == before


def test_online_backup_refuses_existing_destination(live_db: Path, tmp_path: Path) -> None:
    dest = tmp_path / "exists.db"
    dest.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        DataStore.online_backup(live_db, dest)
    assert dest.read_bytes() == b"keep me"


def test_backup_of_missing_database_fails(tmp_path: Path, backup_root: Path) -> None:
    with pytest.raises(BackupError, match="not found"):
        create_backup(tmp_path / "nope.db", backup_root)


def test_online_backup_captures_uncheckpointed_wal_writes(live_db: Path, backup_root: Path) -> None:
    """Writes still sitting in the -wal file (open writer, no checkpoint) are included —
    the reason a plain file copy of applications.db is unsafe."""
    writer = sqlite3.connect(live_db)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO application (company, role, source_portal, application_method, "
        "applied_date, current_status, thread_ids, is_false_positive, created_at, updated_at) "
        "VALUES ('WalOnly', 'r', 'LinkedIn', 'Unknown', '2026-01-01', 'Applied', '[]', 0, "
        "'2026-01-01', '2026-01-01')"
    )
    writer.commit()
    assert Path(f"{live_db}-wal").stat().st_size > 0
    try:
        result = create_backup(live_db, backup_root, now=NOW)
    finally:
        writer.close()
    assert result.manifest.application_count == 5
    assert not Path(f"{result.database_path}-wal").exists()


def test_backup_of_unversioned_legacy_database(tmp_path: Path, backup_root: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    seed_legacy_rows(legacy)
    result = create_backup(legacy, backup_root, now=NOW)
    assert result.manifest.schema_revision is None
    assert result.manifest.application_count == 3


# ------------------------------------------------------------------ #
# Validation: checksum, corruption, manifest                           #
# ------------------------------------------------------------------ #


def test_validate_accepts_good_backup(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    assert validate_backup(result.path) == result.manifest


def test_checksum_mismatch_is_rejected(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    manifest_path = result.path / MANIFEST_NAME
    data = json.loads(manifest_path.read_text())
    data["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(BackupValidationError, match="checksum"):
        validate_backup(result.path)


def test_corrupted_backup_is_rejected(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    db_copy = result.database_path
    raw = bytearray(db_copy.read_bytes())
    raw[4096 + 100 : 4096 + 400] = b"\xff" * 300  # trash part of page 2
    db_copy.write_bytes(bytes(raw))
    with pytest.raises(BackupValidationError, match="checksum"):
        validate_backup(result.path)


def test_corruption_with_matching_checksum_fails_integrity(
    live_db: Path, backup_root: Path
) -> None:
    """Even if an attacker or bug rewrote the manifest to match, SQLite integrity fails."""
    result = create_backup(live_db, backup_root, now=NOW)
    db_copy = result.database_path
    raw = bytearray(db_copy.read_bytes())
    raw[4096 + 8 : 4096 + 64] = b"\x00" * 56  # break a b-tree page header
    db_copy.write_bytes(bytes(raw))
    manifest_path = result.path / MANIFEST_NAME
    data = json.loads(manifest_path.read_text())
    data["sha256"] = sha256_file(db_copy)
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(BackupValidationError, match="integrity"):
        validate_backup(result.path)


def test_truncated_backup_is_rejected(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    db_copy = result.database_path
    db_copy.write_bytes(db_copy.read_bytes()[:1000])
    with pytest.raises(BackupValidationError, match="size"):
        validate_backup(result.path)


def test_missing_manifest_is_rejected(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    (result.path / MANIFEST_NAME).unlink()
    with pytest.raises(BackupValidationError, match="No manifest"):
        validate_backup(result.path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("sha256"), "missing fields"),
        (lambda d: d.update(size_bytes="12"), "wrong type"),
        (lambda d: d.update(tool="something-else"), "not written by"),
        (lambda d: d.update(format_version=99), "format_version"),
        (lambda d: d.update(integrity_check="corrupt"), "failed integrity"),
        (lambda d: d.update(database_file="../../etc/passwd"), "database_file"),
        (lambda d: d.update(created_at="yesterday"), "ISO timestamp"),
    ],
)
def test_invalid_manifest_is_rejected(live_db: Path, backup_root: Path, mutate, message) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    manifest_path = result.path / MANIFEST_NAME
    data = json.loads(manifest_path.read_text())
    mutate(data)
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(BackupValidationError, match=message):
        load_manifest(result.path)


def test_unreadable_manifest_is_rejected(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    (result.path / MANIFEST_NAME).write_text("{not json")
    with pytest.raises(BackupValidationError, match="unreadable"):
        load_manifest(result.path)


# ------------------------------------------------------------------ #
# Restore                                                              #
# ------------------------------------------------------------------ #


def test_safe_restore_to_new_destination(live_db: Path, backup_root: Path, tmp_path: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    dest = tmp_path / "restored" / "applications.db"
    dest.parent.mkdir()
    restored = restore_backup(result.path, dest)
    assert restored.destination == dest
    assert restored.moved_aside == []
    assert DataStore.integrity_check(dest) == ["ok"]
    store = DataStore(dest)
    _, total = store.get_applications(ApplicationFilter())
    assert total == 4
    assert store.find_application_by_thread_id("thread-2").company == "Co2"
    store.close()
    assert not list(dest.parent.glob(".*.tmp"))


def test_restore_refuses_existing_destination(
    live_db: Path, backup_root: Path, tmp_path: Path
) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    dest = tmp_path / "current.db"
    dest.write_bytes(b"precious")
    with pytest.raises(RestoreRefused, match="already exists"):
        restore_backup(result.path, dest)
    assert dest.read_bytes() == b"precious"


def test_restore_refuses_when_only_wal_sidecar_exists(
    live_db: Path, backup_root: Path, tmp_path: Path
) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    dest = tmp_path / "current.db"
    Path(f"{dest}-wal").write_bytes(b"stale wal")
    with pytest.raises(RestoreRefused):
        restore_backup(result.path, dest)


def test_forced_restore_moves_existing_files_aside(
    live_db: Path, backup_root: Path, tmp_path: Path
) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    dest = tmp_path / "current.db"
    dest.write_bytes(b"precious")
    Path(f"{dest}-wal").write_bytes(b"wal")
    restored = restore_backup(result.path, dest, force=True, now=NOW)
    assert sorted(p.name for p in restored.moved_aside) == [
        "current.db-wal.pre-restore-20261009T120000Z",
        "current.db.pre-restore-20261009T120000Z",
    ]
    assert (tmp_path / "current.db.pre-restore-20261009T120000Z").read_bytes() == b"precious"
    assert not Path(f"{dest}-wal").exists()
    assert DataStore.count_rows_readonly(dest)["application"] == 4


def test_forced_restore_never_overwrites_an_earlier_aside_copy(
    live_db: Path, backup_root: Path, tmp_path: Path
) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    dest = tmp_path / "current.db"
    dest.write_bytes(b"current")
    (tmp_path / "current.db.pre-restore-20261009T120000Z").write_bytes(b"older")
    with pytest.raises(RestoreRefused):
        restore_backup(result.path, dest, force=True, now=NOW)
    assert dest.read_bytes() == b"current"


def test_restore_rejects_corrupt_backup_and_leaves_destination_alone(
    live_db: Path, backup_root: Path, tmp_path: Path
) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    result.database_path.write_bytes(b"garbage")
    dest = tmp_path / "current.db"
    dest.write_bytes(b"current")
    with pytest.raises(BackupValidationError):
        restore_backup(result.path, dest, force=True)
    assert dest.read_bytes() == b"current"


def test_restore_requires_existing_destination_directory(
    live_db: Path, backup_root: Path, tmp_path: Path
) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    with pytest.raises(RestoreRefused, match="does not exist"):
        restore_backup(result.path, tmp_path / "missing-dir" / "x.db")


# ------------------------------------------------------------------ #
# Verify (temporary restore + DataStore)                               #
# ------------------------------------------------------------------ #


def test_verify_restores_into_temp_and_opens_via_datastore(
    live_db: Path, backup_root: Path
) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    before = sorted(p.name for p in result.path.parent.parent.rglob("*"))
    verified = verify_backup(result.path)
    assert verified.application_count == 4
    assert verified.restored_schema_state == SchemaState.CURRENT.value
    assert verified.migrated_to is None
    assert sorted(p.name for p in result.path.parent.parent.rglob("*")) == before


def test_verify_proves_a_legacy_backup_migrates(tmp_path: Path, backup_root: Path) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    seed_legacy_rows(legacy)
    result = create_backup(legacy, backup_root, now=NOW)
    verified = verify_backup(result.path)
    assert verified.restored_schema_state == SchemaState.UNVERSIONED.value
    assert verified.migrated_to == head_revision()
    assert verified.application_count == 3
    assert verified.prospect_count == 1
    # The backup itself is untouched by the trial migration.
    assert read_status(result.database_path).state is SchemaState.UNVERSIONED
    validate_backup(result.path)


def test_verify_without_migration_check_rejects_legacy_backup(
    tmp_path: Path, backup_root: Path
) -> None:
    legacy = build_legacy_database(tmp_path / "legacy.db")
    result = create_backup(legacy, backup_root, now=NOW)
    with pytest.raises(BackupValidationError, match="cannot be opened"):
        verify_backup(result.path, check_migration=False)


def test_verify_rejects_corrupt_backup(live_db: Path, backup_root: Path) -> None:
    result = create_backup(live_db, backup_root, now=NOW)
    result.database_path.write_bytes(b"not sqlite")
    with pytest.raises(BackupValidationError):
        verify_backup(result.path)


# ------------------------------------------------------------------ #
# Listing and retention                                                #
# ------------------------------------------------------------------ #


def test_list_latest_and_prune(live_db: Path, backup_root: Path) -> None:
    made = [create_backup(live_db, backup_root, now=NOW.replace(hour=h)).path for h in (1, 2, 3, 4)]
    (backup_root / "incomplete").mkdir()  # no manifest: never listed, never pruned
    assert list_backups(backup_root) == made
    assert latest_backup(backup_root) == made[-1]
    removed = prune_backups(backup_root, keep=2)
    assert removed == made[:2]
    assert list_backups(backup_root) == made[2:]
    assert (backup_root / "incomplete").is_dir()
    with pytest.raises(ValueError):
        prune_backups(backup_root, keep=0)


def test_prune_skips_directories_with_foreign_manifests(live_db: Path, backup_root: Path) -> None:
    foreign = backup_root / "00000000T000000Z-other"
    foreign.mkdir(parents=True)
    (foreign / MANIFEST_NAME).write_text('{"tool": "not-ours"}')
    create_backup(live_db, backup_root, now=NOW)
    prune_backups(backup_root, keep=1)
    assert foreign.is_dir()


def test_latest_backup_is_by_creation_time_not_label(live_db: Path, backup_root: Path) -> None:
    """Regression: backups in the same second sorted by label made `--latest` pick an older
    pre-migration backup over a newer manual one."""
    older = create_backup(live_db, backup_root, label="zzz-older", now=NOW)
    newer = create_backup(live_db, backup_root, label="aaa-newer", now=NOW.replace(microsecond=5))
    assert older.path.name[:16] == newer.path.name[:16]  # same second
    assert list_backups(backup_root) == [older.path, newer.path]
    assert latest_backup(backup_root) == newer.path
    assert prune_backups(backup_root, keep=1) == [older.path]


def test_backup_with_merged_records_verifies_restores_and_undoes(
    live_db: Path, backup_root: Path, tmp_path: Path
) -> None:
    """Merged applications are rows too: the manifest, verification and restore keep them,
    and the merge can still be undone on the restored copy."""
    from backend.engine.merge_planner import plan_merge, resolve_field_values

    store = DataStore(live_db)
    state = store.load_merge_state([1, 2])
    state["application_ids"] = [1, 2]
    plan = plan_merge(state, survivor_id=1)
    values = resolve_field_values(plan, {name: 1 for name in plan.conflicts})
    op, _ = store.execute_merge(
        application_ids=[1, 2],
        survivor_id=1,
        field_values=values,
        expected_token=plan.token,
        idempotency_key="backup-merge-0001",
    )
    assert store.get_applications(ApplicationFilter())[1] == 3
    store.close()

    result = create_backup(live_db, backup_root, now=NOW)
    assert result.manifest.application_count == 4
    assert verify_backup(result.path).application_count == 4

    dest = tmp_path / "restored" / "applications.db"
    dest.parent.mkdir()
    restore_backup(result.path, dest)
    restored = DataStore(dest)
    assert restored.get_applications(ApplicationFilter())[1] == 3
    assert restored.find_application_by_thread_id("thread-1").id == 1  # follows the merge
    _, undone_now = restored.undo_merge(op.id)
    assert undone_now
    assert restored.get_applications(ApplicationFilter())[1] == 4
    restored.close()
