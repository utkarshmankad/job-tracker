"""Diagnostic runner — checks DB health, schema, API, and poller on demand.

Run directly:  python -m backend.diagnostics
Or import:    from backend.diagnostics import DiagnosticRunner, run_diagnostics
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class DiagnosticResult:
    name: str
    ok: bool
    detail: str
    checked_at: datetime = field(default_factory=datetime.utcnow)

    def __str__(self) -> str:
        symbol = "✓" if self.ok else "✗"
        return f"  [{symbol}] {self.name}: {self.detail}"


class DiagnosticRunner:
    """Runs a suite of health checks and returns a list of DiagnosticResult objects."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        from backend.config import DB_PATH
        self._db_path = db_path or DB_PATH

    def run_all(self) -> list[DiagnosticResult]:
        results: list[DiagnosticResult] = []
        checks = [
            self._check_db_file,
            self._check_db_connectivity,
            self._check_schema_tables,
            self._check_enum_values,
            self._check_poller_state,
            self._check_config_paths,
        ]
        for check in checks:
            try:
                result = check()
                results.append(result)
                if not result.ok:
                    log.error("diagnostic_check_failed", name=result.name, detail=result.detail)
            except Exception as exc:
                results.append(DiagnosticResult(
                    name=check.__name__.lstrip("_check_"),
                    ok=False,
                    detail=f"Check raised {type(exc).__name__}: {exc}",
                ))
        return results

    # ------------------------------------------------------------------ #
    # Individual checks                                                    #
    # ------------------------------------------------------------------ #

    def _check_db_file(self) -> DiagnosticResult:
        if not self._db_path.exists():
            return DiagnosticResult("db_file", False, f"DB file not found: {self._db_path}")
        size_kb = self._db_path.stat().st_size // 1024
        return DiagnosticResult("db_file", True, f"Exists — {size_kb} KB at {self._db_path}")

    def _check_db_connectivity(self) -> DiagnosticResult:
        try:
            from backend.db.data_store import DataStore, ApplicationFilter
            ds = DataStore(self._db_path)
            _, total = ds.get_applications(ApplicationFilter(page_size=1))
            return DiagnosticResult("db_connectivity", True, f"Connected — {total} applications")
        except Exception as exc:
            return DiagnosticResult("db_connectivity", False, f"Cannot connect: {exc}")

    def _check_schema_tables(self) -> DiagnosticResult:
        import sqlite3
        required = {"application", "statushistory", "suppressrule", "pollerstate", "processedmessage"}
        try:
            conn = sqlite3.connect(str(self._db_path))
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            found = {row[0] for row in cur.fetchall()}
            conn.close()
            missing = required - found
            if missing:
                return DiagnosticResult(
                    "schema_tables", False,
                    f"Missing tables: {', '.join(sorted(missing))}",
                )
            return DiagnosticResult("schema_tables", True, f"All {len(required)} required tables present")
        except Exception as exc:
            return DiagnosticResult("schema_tables", False, f"Schema check failed: {exc}")

    def _check_enum_values(self) -> DiagnosticResult:
        """Ensure current_status column stores enum VALUES, not member NAMES.

        The SAEnum definition uses values_callable to store e.g. 'Applied' not 'APPLIED'.
        If old data was stored using member names, reads would raise LookupError.
        """
        import sqlite3
        from backend.db.models import ApplicationStatus

        valid_values = {e.value for e in ApplicationStatus}
        valid_names = {e.name for e in ApplicationStatus}

        try:
            conn = sqlite3.connect(str(self._db_path))
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT current_status FROM application")
            rows = [row[0] for row in cur.fetchall()]
            conn.close()

            name_format = [r for r in rows if r in valid_names and r not in valid_values]
            unknown = [r for r in rows if r not in valid_values and r not in valid_names]

            if name_format:
                return DiagnosticResult(
                    "enum_values", False,
                    f"current_status stored as enum NAMES (will cause LookupError): "
                    f"{name_format}. Run migration to convert to values.",
                )
            if unknown:
                return DiagnosticResult(
                    "enum_values", False,
                    f"Unrecognised current_status values: {unknown}",
                )
            return DiagnosticResult(
                "enum_values", True,
                f"All {len(rows)} distinct status values are in value format",
            )
        except Exception as exc:
            return DiagnosticResult("enum_values", False, f"Enum check failed: {exc}")

    def _check_poller_state(self) -> DiagnosticResult:
        try:
            from backend.db.data_store import DataStore
            ds = DataStore(self._db_path)
            state = ds.get_poller_state()

            if state.status in ("AUTH_ERROR", "AUTH_REQUIRED"):
                return DiagnosticResult(
                    "poller_state", False,
                    f"Auth error — run: python backend/setup_wizard.py reauth",
                )
            if state.status == "API_ERROR":
                return DiagnosticResult(
                    "poller_state", False,
                    f"API error: {state.error_message or 'unknown'}",
                )
            if state.last_sync_at:
                age_min = int((datetime.utcnow() - state.last_sync_at).total_seconds() / 60)
                if age_min > 15:
                    return DiagnosticResult(
                        "poller_state", False,
                        f"Last synced {age_min} min ago — scheduler may be stopped",
                    )
                return DiagnosticResult(
                    "poller_state", True,
                    f"Status={state.status}, last sync {age_min} min ago",
                )
            return DiagnosticResult(
                "poller_state", True,
                f"Status={state.status}, never synced (first run pending)",
            )
        except Exception as exc:
            return DiagnosticResult("poller_state", False, f"Poller state check failed: {exc}")

    def _check_config_paths(self) -> DiagnosticResult:
        from backend.config import CREDENTIALS_PATH, PORTAL_RULES_PATH
        issues: list[str] = []
        if not PORTAL_RULES_PATH.exists():
            issues.append(f"portal_rules.yaml missing at {PORTAL_RULES_PATH}")
        if not CREDENTIALS_PATH.exists():
            issues.append(f"client_secret.json missing at {CREDENTIALS_PATH}")
        if issues:
            return DiagnosticResult("config_paths", False, "; ".join(issues))
        return DiagnosticResult("config_paths", True, "All config paths present")


def run_diagnostics(db_path: Optional[Path] = None) -> bool:
    """Run all diagnostics and print results. Returns True if all checks passed."""
    runner = DiagnosticRunner(db_path)
    results = runner.run_all()

    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)

    print(f"\nDiagnostic Report — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
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
