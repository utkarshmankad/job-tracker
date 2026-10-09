"""Tests for backend/db/recovery_cli.py (scripts/*_database.py, scripts/verify_backup.py)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from backend.db.backup import create_backup, list_backups, load_manifest
from backend.db.data_store import DataStore
from backend.db.recovery_cli import (
    backup_command,
    migrate_group,
    restore_command,
    verify_command,
)
from backend.db.schema import SchemaState, head_revision, read_status
from tests.conftest import build_legacy_database, seed_legacy_rows

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Point the configured paths at a temp JOB_TRACKER_DIR — never the real database."""
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "db": data / "applications.db",
        "backups": data / "backups",
        "flag": data / "MAINTENANCE",
    }
    monkeypatch.setattr("backend.config.DB_PATH", paths["db"])
    monkeypatch.setattr("backend.config.BACKUP_DIR", paths["backups"])
    monkeypatch.setattr("backend.config.MAINTENANCE_FLAG_PATH", paths["flag"])
    return paths


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_backup_command_creates_and_verifies(env, runner) -> None:
    DataStore(env["db"]).close()
    result = runner.invoke(backup_command, ["--label", "nightly"])
    assert result.exit_code == 0, result.output
    assert "Verified:" in result.output
    [made] = list_backups(env["backups"])
    assert made.name.endswith("-nightly")


def test_backup_command_prunes_only_after_verification(env, runner) -> None:
    DataStore(env["db"]).close()
    for label in ("a", "b", "c"):
        assert runner.invoke(backup_command, ["--label", label, "--keep", "2"]).exit_code == 0
    assert [p.name.split("-", 1)[1] for p in list_backups(env["backups"])] == ["b", "c"]
    refused = runner.invoke(backup_command, ["--no-verify", "--keep", "1"])
    assert refused.exit_code != 0
    assert "requires --verify" in refused.output


def test_backup_command_missing_database(env, runner) -> None:
    result = runner.invoke(backup_command, [])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_restore_requires_destination(env, runner, tmp_path) -> None:
    DataStore(env["db"]).close()
    backup = create_backup(env["db"], env["backups"])
    result = runner.invoke(restore_command, [str(backup.path)])
    assert result.exit_code == 2
    assert "--destination" in result.output


def test_restore_to_new_file(env, runner, tmp_path) -> None:
    DataStore(env["db"]).close()
    backup = create_backup(env["db"], env["backups"])
    dest = tmp_path / "copy.db"
    result = runner.invoke(restore_command, [str(backup.path), "--destination", str(dest)])
    assert result.exit_code == 0, result.output
    assert DataStore.integrity_check(dest) == ["ok"]


def test_restore_refuses_existing_destination_without_force(env, runner, tmp_path) -> None:
    DataStore(env["db"]).close()
    backup = create_backup(env["db"], env["backups"])
    dest = tmp_path / "copy.db"
    dest.write_bytes(b"keep")
    result = runner.invoke(restore_command, [str(backup.path), "--destination", str(dest)])
    assert result.exit_code != 0
    assert "--force" in result.output
    assert dest.read_bytes() == b"keep"


def test_restore_over_live_database_needs_quiesced_writers(env, runner) -> None:
    DataStore(env["db"]).close()
    backup = create_backup(env["db"], env["backups"])
    args = [str(backup.path), "--destination", str(env["db"]), "--force"]

    refused = runner.invoke(restore_command, args)
    assert refused.exit_code != 0
    assert "maintenance mode" in refused.output

    env["flag"].touch()
    ok = runner.invoke(restore_command, args)
    assert ok.exit_code == 0, ok.output
    assert "Moved aside:" in ok.output


def test_verify_latest_and_explicit(env, runner) -> None:
    DataStore(env["db"]).close()
    backup = create_backup(env["db"], env["backups"])
    assert runner.invoke(verify_command, ["--latest"]).exit_code == 0
    assert runner.invoke(verify_command, [str(backup.path)]).exit_code == 0
    assert runner.invoke(verify_command, []).exit_code == 2
    assert runner.invoke(verify_command, [str(backup.path), "--latest"]).exit_code == 2


def test_verify_reports_unusable_backup(env, runner) -> None:
    DataStore(env["db"]).close()
    backup = create_backup(env["db"], env["backups"])
    backup.database_path.write_bytes(b"garbage")
    result = runner.invoke(verify_command, [str(backup.path)])
    assert result.exit_code != 0
    assert "NOT usable" in result.output


def test_verify_latest_with_no_backups(env, runner) -> None:
    result = runner.invoke(verify_command, ["--latest"])
    assert result.exit_code != 0
    assert "No backups" in result.output


# ------------------------------------------------------------------ #
# migrate status / upgrade                                             #
# ------------------------------------------------------------------ #


def test_migrate_status_exit_codes(env, runner) -> None:
    assert runner.invoke(migrate_group, ["status"]).exit_code == 2  # missing
    build_legacy_database(env["db"])
    pending = runner.invoke(migrate_group, ["status"])
    assert pending.exit_code == 1
    assert "unversioned" in pending.output
    assert head_revision() in pending.output


def test_migrate_upgrade_refuses_live_database_without_quiescing(env, runner) -> None:
    build_legacy_database(env["db"])
    result = runner.invoke(migrate_group, ["upgrade"])
    assert result.exit_code != 0
    assert "maintenance mode" in result.output
    assert read_status(env["db"]).state is SchemaState.UNVERSIONED
    assert list_backups(env["backups"]) == []


def test_migrate_upgrade_full_sequence(env, runner) -> None:
    build_legacy_database(env["db"])
    seed_legacy_rows(env["db"])
    env["flag"].touch()

    result = runner.invoke(migrate_group, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert "Verified:" in result.output
    assert f"migrates cleanly to {head_revision()}" in result.output
    assert "Integrity:     ok" in result.output
    assert "Migration complete" in result.output
    assert read_status(env["db"]).is_current

    [backup] = list_backups(env["backups"])
    manifest = load_manifest(backup)
    assert manifest.label == "pre-migration"
    assert manifest.schema_revision is None

    again = runner.invoke(migrate_group, ["upgrade"])
    assert again.exit_code == 0
    assert "Nothing to do" in again.output
    assert len(list_backups(env["backups"])) == 1
    assert runner.invoke(migrate_group, ["status"]).exit_code == 0


def test_migrate_upgrade_on_other_file_needs_no_flag(env, runner, tmp_path) -> None:
    other = build_legacy_database(tmp_path / "other.db")
    result = runner.invoke(
        migrate_group,
        ["upgrade", "--db", str(other), "--backup-dir", str(tmp_path / "other-backups")],
    )
    assert result.exit_code == 0, result.output
    assert read_status(other).is_current


def test_migrate_upgrade_aborts_cleanly_if_backup_fails(env, runner, monkeypatch) -> None:
    from backend.db import recovery_cli
    from backend.db.backup import BackupError

    build_legacy_database(env["db"])
    env["flag"].touch()

    def fail(*_a, **_k):
        raise BackupError("disk full")

    monkeypatch.setattr(recovery_cli, "create_backup", fail)
    result = runner.invoke(migrate_group, ["upgrade"])
    assert result.exit_code != 0
    assert "nothing changed" in result.output
    assert read_status(env["db"]).state is SchemaState.UNVERSIONED


def test_migrate_upgrade_rejects_unknown_revision(env, runner) -> None:
    import sqlite3

    DataStore(env["db"]).close()
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE alembic_version SET version_num = '9999_future'")
    conn.commit()
    conn.close()
    env["flag"].touch()
    result = runner.invoke(migrate_group, ["upgrade"])
    assert result.exit_code != 0
    assert "unknown to this release" in result.output


@pytest.mark.parametrize(
    "script",
    ["backup_database.py", "restore_database.py", "verify_backup.py", "migrate_database.py"],
)
def test_scripts_are_runnable_entry_points(script: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), "--help"],
        capture_output=True,
        text=True,
        cwd="/",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout
