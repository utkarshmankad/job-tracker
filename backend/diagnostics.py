"""Diagnostic runner — checks DB health, schema version, data, Gmail credentials and poller.

Run directly:  python -m backend.diagnostics
Or import:    from backend.diagnostics import DiagnosticRunner, run_diagnostics

Diagnostics never change the database: DataStore is opened with SchemaPolicy.INSPECT, so
an outdated or missing schema is reported rather than migrated or created.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from backend.db.models import utc_now

if TYPE_CHECKING:
    from backend.db.data_store import DataStore

log = structlog.get_logger(__name__)


@dataclass
class DiagnosticResult:
    name: str
    ok: bool
    detail: str
    checked_at: datetime = field(default_factory=utc_now)

    def __str__(self) -> str:
        symbol = "✓" if self.ok else "✗"
        return f"  [{symbol}] {self.name}: {self.detail}"


def required_tables() -> set[str]:
    """Every table the current models define (the schema source of truth)."""
    from sqlmodel import SQLModel

    import backend.db.models  # noqa: F401  — registers the tables

    return set(SQLModel.metadata.tables)


class DiagnosticRunner:
    """Runs a suite of health checks and returns a list of DiagnosticResult objects."""

    def __init__(self, db_path: Path | None = None) -> None:
        from backend.config import DB_PATH

        self._db_path = db_path or DB_PATH

    def run_all(self) -> list[DiagnosticResult]:
        results: list[DiagnosticResult] = []
        checks = [
            self._check_db_file,
            self._check_db_connectivity,
            self._check_schema_revision,
            self._check_schema_tables,
            self._check_enum_values,
            self._check_poller_state,
            self._check_config_paths,
            self._check_gmail_credentials,
        ]
        for check in checks:
            try:
                result = check()
            except Exception as exc:
                result = DiagnosticResult(
                    name=check.__name__.removeprefix("_check_"),
                    ok=False,
                    detail=f"Check raised {type(exc).__name__}: {exc}",
                )
            results.append(result)
            if not result.ok:
                log.error("diagnostic_check_failed", name=result.name, detail=result.detail)
        return results

    def _open_store(self) -> DataStore:
        from backend.db.data_store import DataStore
        from backend.db.schema import SchemaPolicy

        if not self._db_path.is_file():
            raise FileNotFoundError(f"DB file not found: {self._db_path}")
        return DataStore(self._db_path, schema_policy=SchemaPolicy.INSPECT)

    # ------------------------------------------------------------------ #
    # Individual checks                                                    #
    # ------------------------------------------------------------------ #

    def _check_db_file(self) -> DiagnosticResult:
        if not self._db_path.exists():
            return DiagnosticResult("db_file", False, f"DB file not found: {self._db_path}")
        size_kb = self._db_path.stat().st_size // 1024
        return DiagnosticResult("db_file", True, f"Exists — {size_kb} KB at {self._db_path}")

    def _check_db_connectivity(self) -> DiagnosticResult:
        from backend.db.data_store import ApplicationFilter

        try:
            ds = self._open_store()
            try:
                _, total = ds.get_applications(ApplicationFilter(page_size=1))
            finally:
                ds.close()
            return DiagnosticResult("db_connectivity", True, f"Connected — {total} applications")
        except Exception as exc:
            return DiagnosticResult("db_connectivity", False, f"Cannot connect: {exc}")

    def _check_schema_revision(self) -> DiagnosticResult:
        from backend.db.schema import read_status

        if not self._db_path.is_file():
            return DiagnosticResult("schema_revision", False, "DB file not found")
        status = read_status(self._db_path)
        if status.is_current:
            return DiagnosticResult(
                "schema_revision", True, f"At head revision {status.head_revision}"
            )
        return DiagnosticResult(
            "schema_revision",
            False,
            f"Database {status.describe()} — run scripts/migrate_database.py "
            "(docs/database-operations.md)",
        )

    def _check_schema_tables(self) -> DiagnosticResult:
        from backend.db.data_store import DataStore
        from backend.db.schema import VERSION_TABLE

        required = required_tables() | {VERSION_TABLE}
        try:
            found = DataStore.inspect_schema_tables(self._db_path)
            missing = required - found
            if missing:
                return DiagnosticResult(
                    "schema_tables",
                    False,
                    f"Missing tables: {', '.join(sorted(missing))}",
                )
            return DiagnosticResult(
                "schema_tables", True, f"All {len(required)} required tables present"
            )
        except Exception as exc:
            return DiagnosticResult("schema_tables", False, f"Schema check failed: {exc}")

    def _check_enum_values(self) -> DiagnosticResult:
        """Ensure current_status column stores enum VALUES, not member NAMES.

        The SAEnum definition uses values_callable to store e.g. 'Applied' not 'APPLIED'.
        If old data was stored using member names, reads would raise LookupError.
        """
        from backend.db.models import ApplicationStatus

        valid_values = {e.value for e in ApplicationStatus}
        valid_names = {e.name for e in ApplicationStatus}

        try:
            ds = self._open_store()
            try:
                rows = ds.get_raw_status_values()
            finally:
                ds.close()

            name_format = [r for r in rows if r in valid_names and r not in valid_values]
            unknown = [r for r in rows if r not in valid_values and r not in valid_names]

            if name_format:
                return DiagnosticResult(
                    "enum_values",
                    False,
                    f"current_status stored as enum NAMES (will cause LookupError): "
                    f"{name_format}. Run migration to convert to values.",
                )
            if unknown:
                return DiagnosticResult(
                    "enum_values",
                    False,
                    f"Unrecognised current_status values: {unknown}",
                )
            return DiagnosticResult(
                "enum_values",
                True,
                f"All {len(rows)} distinct status values are in value format",
            )
        except Exception as exc:
            return DiagnosticResult("enum_values", False, f"Enum check failed: {exc}")

    def _check_poller_state(self) -> DiagnosticResult:
        try:
            ds = self._open_store()
            try:
                state = ds.get_poller_state()
            finally:
                ds.close()

            if state.status in ("AUTH_ERROR", "AUTH_REQUIRED"):
                return DiagnosticResult(
                    "poller_state",
                    False,
                    "Auth error — run: python backend/setup_wizard.py reauth",
                )
            if state.status == "API_ERROR":
                return DiagnosticResult(
                    "poller_state",
                    False,
                    f"API error: {state.error_message or 'unknown'}",
                )
            if state.last_sync_at:
                age_min = int((utc_now() - state.last_sync_at).total_seconds() / 60)
                if age_min > 15:
                    return DiagnosticResult(
                        "poller_state",
                        False,
                        f"Last synced {age_min} min ago — scheduler may be stopped",
                    )
                return DiagnosticResult(
                    "poller_state",
                    True,
                    f"Status={state.status}, last sync {age_min} min ago",
                )
            return DiagnosticResult(
                "poller_state",
                True,
                f"Status={state.status}, never synced (first run pending)",
            )
        except Exception as exc:
            return DiagnosticResult("poller_state", False, f"Poller state check failed: {exc}")

    def _check_config_paths(self) -> DiagnosticResult:
        from backend.config import PORTAL_RULES_PATH

        if not PORTAL_RULES_PATH.exists():
            return DiagnosticResult(
                "config_paths", False, f"portal_rules.yaml missing at {PORTAL_RULES_PATH}"
            )
        return DiagnosticResult("config_paths", True, "All config paths present")

    def _check_gmail_credentials(self) -> DiagnosticResult:
        """Where Gmail credentials come from. An environment token (Fly) needs no
        client_secret.json for polling; that file only matters for web re-auth."""
        from backend.poller.gmail_poller import GmailPoller

        report = GmailPoller.describe_credentials()
        return DiagnosticResult("gmail_credentials", report.ok, report.detail)


def run_diagnostics(db_path: Path | None = None) -> bool:
    """Run all diagnostics and print a human-readable report to the terminal.

    print() here is intentional — this is the CLI's own report output for a human
    operator, not application logging. structlog.log.info still records the
    machine-readable summary below for anything that parses backend logs.

    Returns True if all checks passed.
    """
    runner = DiagnosticRunner(db_path)
    results = runner.run_all()

    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    log.info("diagnostics_complete", passed=passed, failed=failed)

    print(f"\nDiagnostic Report — {utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 60)
    for result in results:
        print(result)
    print("=" * 60)
    print(f"  {passed} passed, {failed} failed\n")

    if failed:
        print("ACTION REQUIRED — fix the issues above before starting the backend.\n")
    return failed == 0


if __name__ == "__main__":
    ok = run_diagnostics()
    sys.exit(0 if ok else 1)
