"""Schema versioning: Alembic revision status, upgrades, and the startup policy.

Alembic (backend/db/alembic) is the only way the schema changes. DataStore asks this module
what state a database is in and applies the startup policy:

- ``AUTO``    (development default): create an empty database; back up then upgrade an
               outdated one.
- ``VERIFY``  (production): create an empty database; refuse an outdated one with
               SchemaOutdatedError so the API starts in maintenance mode instead of
               migrating unattended.
- ``INSPECT`` (diagnostics, backup verification): never change the schema.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, pool

from backend import config as app_config

log = structlog.get_logger(__name__)

ALEMBIC_DIR = Path(__file__).parent / "alembic"
VERSION_TABLE = "alembic_version"


class SchemaPolicy(enum.StrEnum):
    AUTO = "auto"
    VERIFY = "verify"
    INSPECT = "inspect"


class SchemaState(enum.StrEnum):
    EMPTY = "empty"  # no tables at all
    UNVERSIONED = "unversioned"  # tables exist but no alembic_version (pre-Phase-1 database)
    OUTDATED = "outdated"  # known revision older than head
    CURRENT = "current"  # at head
    UNKNOWN = "unknown"  # revision not known to this release (newer code ran, or corrupt)


@dataclass(frozen=True)
class SchemaStatus:
    state: SchemaState
    current_revision: str | None
    head_revision: str

    @property
    def is_current(self) -> bool:
        return self.state is SchemaState.CURRENT

    @property
    def needs_upgrade(self) -> bool:
        return self.state in (SchemaState.UNVERSIONED, SchemaState.OUTDATED)

    def describe(self) -> str:
        current = self.current_revision or "none"
        return f"schema {self.state.value} (revision {current}, head {self.head_revision})"


class SchemaOutdatedError(RuntimeError):
    """The database needs an operator-run migration before the app may use it."""

    def __init__(self, status: SchemaStatus) -> None:
        super().__init__(
            f"Database {status.describe()}. Run scripts/migrate_database.py "
            "(see docs/database-operations.md)."
        )
        self.status = status


def default_policy() -> SchemaPolicy:
    if app_config.APP_ENV == "production":
        return SchemaPolicy.VERIFY
    return SchemaPolicy.AUTO if app_config.DB_AUTO_MIGRATE else SchemaPolicy.VERIFY


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.attributes["configure_logger"] = False
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("No Alembic revisions found")
    return head


def known_revisions() -> set[str]:
    script = ScriptDirectory.from_config(alembic_config())
    return {rev.revision for rev in script.walk_revisions()}


def get_status(engine: Engine) -> SchemaStatus:
    head = head_revision()
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        current = (
            MigrationContext.configure(conn).get_current_revision()
            if VERSION_TABLE in tables
            else None
        )
    if current is None:
        state = SchemaState.EMPTY if not (tables - {VERSION_TABLE}) else SchemaState.UNVERSIONED
    elif current == head:
        state = SchemaState.CURRENT
    elif current in known_revisions():
        state = SchemaState.OUTDATED
    else:
        state = SchemaState.UNKNOWN
    return SchemaStatus(state=state, current_revision=current, head_revision=head)


def read_status(db_path: Path) -> SchemaStatus:
    """Schema status of a database file without modifying it (opened read-only)."""
    engine = readonly_engine(db_path)
    try:
        return get_status(engine)
    finally:
        engine.dispose()


def readonly_engine(db_path: Path) -> Engine:
    return create_engine(
        f"sqlite:///file:{db_path}?mode=ro&uri=true",
        poolclass=pool.NullPool,
    )


def upgrade(engine: Engine, revision: str = "head") -> SchemaStatus:
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
    status = get_status(engine)
    log.info("schema_upgraded", revision=status.current_revision)
    return status


def ancestors(revision: str) -> list[str]:
    """Revisions strictly below `revision` in the chain, newest first."""
    script = ScriptDirectory.from_config(alembic_config())
    chain = [rev.revision for rev in script.iterate_revisions(revision, "base")]
    return chain[1:]


def stamp(engine: Engine, revision: str) -> SchemaStatus:
    """Rewrite only the recorded revision (no schema change). Used for the documented
    rollback to an older release on an expand-only schema; callers must have checked that
    `revision` is an ancestor of the current one."""
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.stamp(cfg, revision)
    status = get_status(engine)
    log.warning("schema_stamped", revision=status.current_revision)
    return status


def prepare(engine: Engine, db_path: Path, policy: SchemaPolicy) -> SchemaStatus:
    """Apply the startup policy. Returns the resulting status or raises SchemaOutdatedError."""
    status = get_status(engine)
    if policy is SchemaPolicy.INSPECT or status.is_current:
        return status
    if status.state is SchemaState.EMPTY:
        # Nothing to lose: creating the schema in an empty database is always safe.
        return upgrade(engine)
    if status.state is SchemaState.UNKNOWN or policy is SchemaPolicy.VERIFY:
        raise SchemaOutdatedError(status)

    # AUTO and the database holds data: never migrate without a verified backup first.
    from backend.db.backup import create_backup

    backup = create_backup(
        db_path, db_path.parent / app_config.BACKUP_DIR_NAME, label="auto-pre-migration"
    )
    log.warning(
        "schema_auto_migrating",
        from_revision=status.current_revision,
        to_revision=status.head_revision,
        backup=str(backup.path),
    )
    return upgrade(engine)
