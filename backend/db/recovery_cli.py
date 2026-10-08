"""Operator commands for backups, restores, verification and schema migrations.

Entry points (thin wrappers in scripts/):
    scripts/backup_database.py   create a verified backup (optionally prune old ones)
    scripts/restore_database.py  restore a validated backup to an explicit destination
    scripts/verify_backup.py     restore into a temp dir and open through DataStore
    scripts/migrate_database.py  `status` / `upgrade` (backup → verify → migrate → check)

Human-facing output goes through click.echo; operational events are logged via structlog in
backend/db/backup.py and backend/db/schema.py. See docs/database-operations.md.
"""

from __future__ import annotations

from pathlib import Path

import click

from backend import config as app_config
from backend.db.backup import (
    BackupError,
    BackupResult,
    BackupValidationError,
    RestoreRefused,
    create_backup,
    latest_backup,
    prune_backups,
    restore_backup,
    verify_backup,
)
from backend.db.data_store import DataStore
from backend.db.schema import SchemaPolicy, SchemaState, read_status

# Checks that must pass after a migration; the rest (poller freshness, Gmail credentials)
# are expected to look stale while the service is quiesced and are shown for information.
CRITICAL_CHECKS = ("db_connectivity", "schema_revision", "schema_tables", "enum_values")

_db_option = click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Database file (default: the configured DB_PATH).",
)
_backup_dir_option = click.option(
    "--backup-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Backup root directory (default: <JOB_TRACKER_DIR>/backups).",
)


def _db(db_path: Path | None) -> Path:
    return db_path or app_config.DB_PATH


def _backup_root(backup_dir: Path | None) -> Path:
    return backup_dir or app_config.BACKUP_DIR


def _require_quiesced(target: Path, allow_live: bool, action: str) -> None:
    """Refuse to change the live database while the API may be writing to it."""
    if target.resolve() != app_config.DB_PATH.resolve():
        return
    if app_config.MAINTENANCE_FLAG_PATH.exists() or allow_live:
        return
    raise click.ClickException(
        f"Refusing to {action} the live database while writers may be running. Put the app "
        f"in maintenance mode first (create {app_config.MAINTENANCE_FLAG_PATH} and restart "
        "it), or stop the API and pass --allow-live. See docs/database-operations.md."
    )


def _echo_backup(result: BackupResult) -> None:
    m = result.manifest
    click.echo(f"Backup:        {result.path}")
    click.echo(f"Created:       {m.created_at}")
    click.echo(f"Schema:        {m.schema_revision or 'unversioned'}")
    click.echo(f"Applications:  {m.application_count}")
    click.echo(f"Size:          {m.size_bytes} bytes")
    click.echo(f"SHA-256:       {m.sha256}")


def _verify_and_echo(path: Path, check_migration: bool = True) -> None:
    result = verify_backup(path, check_migration=check_migration)
    migrated = f", migrates cleanly to {result.migrated_to}" if result.migrated_to else ""
    click.echo(
        f"Verified:      restored to a temp dir and opened via DataStore "
        f"({result.application_count} applications, {result.status_history_count} status "
        f"changes, {result.prospect_count} prospects{migrated})"
    )


# ------------------------------------------------------------------ #
# backup / restore / verify                                            #
# ------------------------------------------------------------------ #


@click.command("backup")
@_db_option
@_backup_dir_option
@click.option("--label", default="manual", show_default=True, help="Short tag in the name.")
@click.option("--verify/--no-verify", default=True, show_default=True)
@click.option(
    "--keep",
    type=click.IntRange(min=1),
    default=None,
    help="After a verified backup, delete the oldest backups so this many remain.",
)
def backup_command(
    db_path: Path | None,
    backup_dir: Path | None,
    label: str,
    verify: bool,
    keep: int | None,
) -> None:
    """Create a backup with SQLite's online backup API, plus a manifest."""
    try:
        result = create_backup(_db(db_path), _backup_root(backup_dir), label=label)
        _echo_backup(result)
        if verify:
            _verify_and_echo(result.path)
    except (BackupError, BackupValidationError, RestoreRefused) as exc:
        raise click.ClickException(str(exc)) from exc
    if keep is not None:
        if not verify:
            raise click.ClickException(
                "--keep requires --verify (never prune after an unverified backup)"
            )
        for removed in prune_backups(_backup_root(backup_dir), keep):
            click.echo(f"Pruned:        {removed}")


@click.command("restore")
@click.argument("backup", type=click.Path(path_type=Path, file_okay=False, exists=True))
@click.option(
    "--destination",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Database file to create. Required — there is no default.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Destination exists: move it (and -wal/-shm) aside to *.pre-restore-<ts>, then restore.",
)
@click.option(
    "--allow-live",
    is_flag=True,
    help="Restore onto the configured DB_PATH without the maintenance flag (API stopped).",
)
def restore_command(backup: Path, destination: Path, force: bool, allow_live: bool) -> None:
    """Validate BACKUP (manifest, checksum, integrity) and restore it to --destination."""
    _require_quiesced(destination, allow_live, "restore over")
    try:
        result = restore_backup(backup, destination, force=force)
    except (BackupValidationError, RestoreRefused) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Restored:      {result.destination}")
    click.echo(f"From backup:   {backup} ({result.manifest.created_at})")
    click.echo(f"Schema:        {result.manifest.schema_revision or 'unversioned'}")
    click.echo(f"Applications:  {result.manifest.application_count}")
    for aside in result.moved_aside:
        click.echo(f"Moved aside:   {aside}")


@click.command("verify")
@click.argument(
    "backup", required=False, type=click.Path(path_type=Path, file_okay=False, exists=True)
)
@click.option("--latest", is_flag=True, help="Verify the newest backup in --backup-dir.")
@_backup_dir_option
@click.option(
    "--migration-check/--no-migration-check",
    default=True,
    show_default=True,
    help="If the backup predates the current schema, prove it migrates (temp copy only).",
)
def verify_command(
    backup: Path | None, latest: bool, backup_dir: Path | None, migration_check: bool
) -> None:
    """Restore BACKUP into a temporary directory and open it through DataStore."""
    if bool(backup) == latest:
        raise click.UsageError("Give exactly one of BACKUP or --latest")
    target = backup or latest_backup(_backup_root(backup_dir))
    if target is None:
        raise click.ClickException(f"No backups found in {_backup_root(backup_dir)}")
    try:
        _verify_and_echo(target, check_migration=migration_check)
    except (BackupValidationError, RestoreRefused) as exc:
        raise click.ClickException(f"Backup {target} is NOT usable: {exc}") from exc
    click.echo(f"OK:            {target}")


# ------------------------------------------------------------------ #
# migrations                                                           #
# ------------------------------------------------------------------ #


@click.group("migrate")
def migrate_group() -> None:
    """Inspect and apply schema migrations."""


@migrate_group.command("status")
@_db_option
def migrate_status(db_path: Path | None) -> None:
    """Show the schema revision. Exit 0 when current, 1 when an upgrade is pending,
    2 when the database is missing or its revision is unknown to this release."""
    target = _db(db_path)
    if not target.is_file():
        click.echo(f"Database:  {target} (not found)")
        raise SystemExit(2)
    status = read_status(target)
    click.echo(f"Database:  {target}")
    click.echo(f"State:     {status.state.value}")
    click.echo(f"Revision:  {status.current_revision or 'none'}")
    click.echo(f"Head:      {status.head_revision}")
    if status.is_current:
        return
    raise SystemExit(1 if status.needs_upgrade else 2)


@migrate_group.command("upgrade")
@_db_option
@_backup_dir_option
@click.option(
    "--allow-live",
    is_flag=True,
    help="Migrate the configured DB_PATH without the maintenance flag (API stopped).",
)
def migrate_upgrade(db_path: Path | None, backup_dir: Path | None, allow_live: bool) -> None:
    """Back up, verify the backup (including a trial migration of a temp copy), migrate,
    then run integrity and application diagnostics."""
    from backend.diagnostics import DiagnosticRunner

    target = _db(db_path)
    if not target.is_file():
        raise click.ClickException(f"Database not found: {target}")
    status = read_status(target)
    click.echo(f"Current:       {status.describe()}")
    if status.is_current:
        click.echo("Nothing to do.")
        return
    if status.state is SchemaState.UNKNOWN:
        raise click.ClickException(
            "Database revision is unknown to this release (was it migrated by newer code?). "
            "Deploy the matching release or restore a backup."
        )
    _require_quiesced(target, allow_live, "migrate")

    try:
        backup = create_backup(target, _backup_root(backup_dir), label="pre-migration")
        _echo_backup(backup)
        _verify_and_echo(backup.path)
    except (BackupError, BackupValidationError, RestoreRefused) as exc:
        raise click.ClickException(f"Pre-migration backup failed; nothing changed: {exc}") from exc

    store = DataStore(target, schema_policy=SchemaPolicy.INSPECT)
    try:
        after = store.upgrade_schema()
    finally:
        store.close()
    click.echo(f"Migrated:      {after.describe()}")

    rollback_hint = (
        f"Roll back with: scripts/restore_database.py {backup.path} --destination {target} --force"
    )
    problems = DataStore.integrity_check(target, read_only=False)
    if problems != ["ok"]:
        raise click.ClickException(
            f"Integrity check FAILED after migration: {problems[:5]}. {rollback_hint}"
        )
    click.echo("Integrity:     ok")

    results = DiagnosticRunner(target).run_all()
    for result in results:
        click.echo(str(result))
    failed = [r.name for r in results if r.name in CRITICAL_CHECKS and not r.ok]
    if failed:
        raise click.ClickException(
            f"Critical diagnostics failed: {', '.join(failed)}. {rollback_hint}"
        )
    click.echo(
        "Migration complete. Remove the maintenance flag and restart the API to resume service."
    )


@migrate_group.command("stamp")
@click.argument("revision")
@_db_option
@_backup_dir_option
@click.option(
    "--allow-live",
    is_flag=True,
    help="Stamp the configured DB_PATH without the maintenance flag (API stopped).",
)
def migrate_stamp(
    revision: str, db_path: Path | None, backup_dir: Path | None, allow_live: bool
) -> None:
    """Record an OLDER revision without changing the schema, so an older release (which
    refuses unknown revisions) can run on an expand-only schema. Downward only; takes and
    verifies a backup first. Re-running `upgrade` later is safe: revisions are idempotent."""
    from backend.db import schema as schema_module

    target = _db(db_path)
    if not target.is_file():
        raise click.ClickException(f"Database not found: {target}")
    status = read_status(target)
    if status.current_revision is None or status.state is SchemaState.UNKNOWN:
        raise click.ClickException(f"Cannot stamp a database that is {status.state.value}")
    if revision not in schema_module.ancestors(status.current_revision):
        raise click.ClickException(
            f"{revision} is not older than the current revision {status.current_revision}; "
            "stamp only moves the recorded version down (use `upgrade` to move up)."
        )
    _require_quiesced(target, allow_live, "stamp")
    try:
        backup = create_backup(target, _backup_root(backup_dir), label="pre-stamp")
        _echo_backup(backup)
        _verify_and_echo(backup.path, check_migration=False)
    except (BackupError, BackupValidationError, RestoreRefused) as exc:
        raise click.ClickException(f"Pre-stamp backup failed; nothing changed: {exc}") from exc
    store = DataStore(target, schema_policy=SchemaPolicy.INSPECT)
    try:
        after = store.stamp_schema(revision)
    finally:
        store.close()
    click.echo(f"Stamped:       {after.describe()} (schema objects unchanged)")
