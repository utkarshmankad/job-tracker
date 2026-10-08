"""Verified SQLite backups: create, validate, restore and verify.

Layout — one directory per backup, never reused::

    <backup root>/<UTC timestamp>-<label>/
        applications.db   standalone copy made with SQLite's online backup API
        manifest.json     written last; a backup without it is incomplete

Every operation refuses to overwrite: a backup directory is created exclusively, restore
refuses an existing destination unless explicitly forced, and a forced restore moves the
existing files aside instead of deleting them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import utc_now
from backend.db.schema import SchemaPolicy, SchemaState, read_status

log = structlog.get_logger(__name__)

BACKUP_TOOL_NAME = "job-tracker-backup"
BACKUP_TOOL_VERSION = "1.0.0"
MANIFEST_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_FILE_NAME = "applications.db"
_LABEL_RE = re.compile(r"[^a-z0-9-]+")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


class BackupError(Exception):
    """A backup could not be created."""


class BackupValidationError(Exception):
    """A backup failed validation and must not be restored."""


class RestoreRefused(Exception):
    """Restore declined to proceed (for example the destination already exists)."""


@dataclass(frozen=True)
class BackupManifest:
    format_version: int
    tool: str
    tool_version: str
    created_at: str
    label: str
    database_file: str
    sha256: str
    size_bytes: int
    schema_revision: str | None
    application_count: int
    table_counts: dict[str, int]
    integrity_check: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: Any) -> BackupManifest:
        if not isinstance(data, dict):
            raise BackupValidationError("manifest is not a JSON object")
        expected: dict[str, type | tuple[type, ...]] = {
            "format_version": int,
            "tool": str,
            "tool_version": str,
            "created_at": str,
            "label": str,
            "database_file": str,
            "sha256": str,
            "size_bytes": int,
            "schema_revision": (str, type(None)),
            "application_count": int,
            "table_counts": dict,
            "integrity_check": str,
        }
        missing = sorted(set(expected) - set(data))
        if missing:
            raise BackupValidationError(f"manifest missing fields: {', '.join(missing)}")
        for key, kind in expected.items():
            value = data[key]
            if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
                raise BackupValidationError(f"manifest field {key!r} has the wrong type")
        if data["format_version"] != MANIFEST_FORMAT_VERSION:
            raise BackupValidationError(
                f"unsupported manifest format_version {data['format_version']}"
            )
        if data["tool"] != BACKUP_TOOL_NAME:
            raise BackupValidationError("manifest was not written by the Job Tracker backup tool")
        if not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
            raise BackupValidationError("manifest sha256 is not a hex SHA-256 digest")
        if data["database_file"] != DATABASE_FILE_NAME:
            raise BackupValidationError("manifest database_file is not applications.db")
        if data["integrity_check"] != "ok":
            raise BackupValidationError("manifest records a failed integrity check")
        try:
            datetime.fromisoformat(data["created_at"])
        except ValueError as exc:
            raise BackupValidationError("manifest created_at is not an ISO timestamp") from exc
        return cls(**{key: data[key] for key in expected})


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest: BackupManifest

    @property
    def database_path(self) -> Path:
        return self.path / DATABASE_FILE_NAME


@dataclass(frozen=True)
class RestoreResult:
    destination: Path
    manifest: BackupManifest
    moved_aside: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class VerifyResult:
    backup: Path
    manifest: BackupManifest
    restored_schema_state: str
    migrated_to: str | None
    application_count: int
    status_history_count: int
    prospect_count: int


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _timestamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def _normalise_label(label: str | None) -> str:
    cleaned = _LABEL_RE.sub("-", (label or "manual").strip().lower()).strip("-")
    return cleaned[:40] or "manual"


def _write_new_file(path: Path, content: str) -> None:
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


# ------------------------------------------------------------------ #
# Create                                                               #
# ------------------------------------------------------------------ #


def create_backup(
    db_path: Path,
    backup_root: Path,
    label: str | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Create a verified backup of `db_path` in a new directory under `backup_root`."""
    if not db_path.is_file():
        raise BackupError(f"Database not found: {db_path}")
    created = now or utc_now()
    name = f"{_timestamp(created)}-{_normalise_label(label)}"
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    target = backup_root / name
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BackupError(f"Backup {target} already exists; refusing to overwrite") from exc

    db_copy = target / DATABASE_FILE_NAME
    DataStore.online_backup(db_path, db_copy)
    os.chmod(db_copy, 0o600)
    _fsync(db_copy)

    problems = DataStore.integrity_check(db_copy)
    if problems != ["ok"]:
        # Leave the directory for inspection; without a manifest it can never be restored.
        raise BackupError(f"Backup copy failed integrity check: {problems[:5]}")

    counts = DataStore.count_rows_readonly(db_copy)
    manifest = BackupManifest(
        format_version=MANIFEST_FORMAT_VERSION,
        tool=BACKUP_TOOL_NAME,
        tool_version=BACKUP_TOOL_VERSION,
        created_at=created.isoformat(),
        label=_normalise_label(label),
        database_file=DATABASE_FILE_NAME,
        sha256=sha256_file(db_copy),
        size_bytes=db_copy.stat().st_size,
        schema_revision=read_status(db_copy).current_revision,
        application_count=counts.get("application", 0),
        table_counts=counts,
        integrity_check="ok",
    )
    manifest_path = target / MANIFEST_NAME
    _write_new_file(manifest_path, manifest.to_json())
    os.chmod(manifest_path, 0o600)
    log.info(
        "database_backup_created",
        backup=name,
        size_bytes=manifest.size_bytes,
        schema_revision=manifest.schema_revision,
    )
    return BackupResult(path=target, manifest=manifest)


# ------------------------------------------------------------------ #
# Validate                                                             #
# ------------------------------------------------------------------ #


def load_manifest(backup_dir: Path) -> BackupManifest:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BackupValidationError(f"No manifest in {backup_dir} (incomplete or not a backup)")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupValidationError(f"Manifest is unreadable: {exc}") from exc
    return BackupManifest.from_dict(data)


def validate_backup(backup_dir: Path) -> BackupManifest:
    """Manifest, size, checksum and SQLite integrity. Raises BackupValidationError."""
    manifest = load_manifest(backup_dir)
    db_copy = backup_dir / manifest.database_file
    if not db_copy.is_file():
        raise BackupValidationError(f"Backup database file missing: {db_copy}")
    if db_copy.stat().st_size != manifest.size_bytes:
        raise BackupValidationError("Backup size does not match the manifest")
    if sha256_file(db_copy) != manifest.sha256:
        raise BackupValidationError("Backup checksum does not match the manifest")
    for suffix in _SQLITE_SIDECARS:
        if Path(f"{db_copy}{suffix}").exists():
            raise BackupValidationError(f"Unexpected {suffix} file next to the backup copy")
    problems = DataStore.integrity_check(db_copy)
    if problems != ["ok"]:
        raise BackupValidationError(f"Backup failed SQLite integrity check: {problems[:5]}")
    return manifest


# ------------------------------------------------------------------ #
# Restore                                                              #
# ------------------------------------------------------------------ #


def restore_backup(
    backup_dir: Path,
    destination: Path,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> RestoreResult:
    """Restore a validated backup to an explicit destination.

    Refuses when the destination (or its -wal/-shm/-journal files) exists unless `force`;
    with `force` the existing files are renamed to `<name>.pre-restore-<timestamp>` — never
    deleted. The copy is built beside the destination, integrity-checked and compared with
    the manifest, then moved into place atomically.
    """
    manifest = validate_backup(backup_dir)
    destination = destination.expanduser()
    if not destination.parent.is_dir():
        raise RestoreRefused(f"Destination directory does not exist: {destination.parent}")

    existing = [
        path
        for path in (destination, *(Path(f"{destination}{s}") for s in _SQLITE_SIDECARS))
        if path.exists()
    ]
    if existing and not force:
        raise RestoreRefused(
            f"{destination} already exists. Re-run with --force to move it aside and restore."
        )

    stamp = _timestamp(now or utc_now())
    staging = destination.parent / f".{destination.name}.restore-{stamp}.tmp"
    if staging.exists():
        raise RestoreRefused(f"Staging file {staging} already exists; remove it and retry")
    try:
        DataStore.online_backup(backup_dir / manifest.database_file, staging)
        os.chmod(staging, 0o600)
        problems = DataStore.integrity_check(staging)
        if problems != ["ok"]:
            raise BackupValidationError(f"Restored copy failed integrity check: {problems[:5]}")
        restored_status = read_status(staging)
        if restored_status.current_revision != manifest.schema_revision:
            raise BackupValidationError("Restored schema revision does not match the manifest")
        counts = DataStore.count_rows_readonly(staging)
        if counts.get("application", 0) != manifest.application_count:
            raise BackupValidationError("Restored application count does not match the manifest")
        _fsync(staging)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise

    moved: list[Path] = []
    for path in existing:
        aside = path.with_name(f"{path.name}.pre-restore-{stamp}")
        if aside.exists():
            staging.unlink(missing_ok=True)
            raise RestoreRefused(f"{aside} already exists; refusing to overwrite it")
        path.rename(aside)
        moved.append(aside)
    os.replace(staging, destination)
    _fsync(destination.parent)
    log.info(
        "database_restored",
        backup=backup_dir.name,
        schema_revision=manifest.schema_revision,
        moved_aside=len(moved),
    )
    return RestoreResult(destination=destination, manifest=manifest, moved_aside=moved)


# ------------------------------------------------------------------ #
# Verify                                                               #
# ------------------------------------------------------------------ #


def verify_backup(backup_dir: Path, *, check_migration: bool = True) -> VerifyResult:
    """Prove a backup is usable: validate it, restore it into a temporary directory, bring
    the copy to the current schema if needed (temporary copy only), and open it through
    DataStore. Nothing outside the temporary directory is touched."""
    manifest = validate_backup(backup_dir)
    with tempfile.TemporaryDirectory(prefix="job-tracker-verify-") as tmp:
        restored = restore_backup(backup_dir, Path(tmp) / DATABASE_FILE_NAME).destination
        status = read_status(restored)
        migrated_to: str | None = None
        if status.state is not SchemaState.CURRENT:
            if not (check_migration and status.needs_upgrade):
                raise BackupValidationError(
                    f"Restored copy is {status.describe()} and cannot be opened by this release"
                )
            store = DataStore(restored, schema_policy=SchemaPolicy.INSPECT)
            migrated_to = store.upgrade_schema().current_revision
            store.close()

        store = DataStore(restored, schema_policy=SchemaPolicy.VERIFY)
        try:
            # Exercise the normal read paths, as the app would on startup.
            _, total = store.get_applications(ApplicationFilter(page_size=1))
            store.get_prospects(limit=1)
            store.get_all_status_history()
            store.get_poller_state()
        finally:
            store.close()
        counts = DataStore.count_rows_readonly(restored)
        history = counts.get("statushistory", 0)
        prospects = counts.get("prospect", 0)
        if total != manifest.application_count:
            raise BackupValidationError(
                f"DataStore sees {total} applications but the manifest records "
                f"{manifest.application_count}"
            )
    log.info("database_backup_verified", backup=backup_dir.name, migrated_to=migrated_to)
    return VerifyResult(
        backup=backup_dir,
        manifest=manifest,
        restored_schema_state=status.state.value,
        migrated_to=migrated_to,
        application_count=total,
        status_history_count=history,
        prospect_count=prospects,
    )


# ------------------------------------------------------------------ #
# Listing and retention                                                #
# ------------------------------------------------------------------ #


def _created_at(backup_dir: Path) -> str:
    """Manifest creation time (microsecond ISO, UTC) — "" if unreadable."""
    try:
        data = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    value = data.get("created_at") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def list_backups(backup_root: Path) -> list[Path]:
    """Backup directories with a manifest, oldest first.

    Ordered by the manifest's microsecond `created_at`, not the directory name: two backups
    taken in the same second (e.g. a pre-migration backup and a manual one) share the
    timestamp prefix, and their labels must not decide which is newest.
    """
    if not backup_root.is_dir():
        return []
    found = [p for p in backup_root.iterdir() if p.is_dir() and (p / MANIFEST_NAME).is_file()]
    return sorted(found, key=lambda p: (_created_at(p), p.name))


def latest_backup(backup_root: Path) -> Path | None:
    backups = list_backups(backup_root)
    return backups[-1] if backups else None


def prune_backups(backup_root: Path, keep: int) -> list[Path]:
    """Delete the oldest complete backups so that at most `keep` remain. Only directories
    whose manifest validates as written by this tool are ever removed."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    removable = []
    for path in list_backups(backup_root):
        try:
            load_manifest(path)
        except BackupValidationError:
            continue
        removable.append(path)
    doomed = removable[:-keep]
    for path in doomed:
        shutil.rmtree(path)
        log.info("database_backup_pruned", backup=path.name)
    return doomed
