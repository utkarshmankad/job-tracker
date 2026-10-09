#!/usr/bin/env python3
"""Dry-run Phase 2 reconciliation audit of a database copy.

Usage: python scripts/reconcile_database.py --help   (see docs/phase-2-reconciliation-report.md)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.reconcile_cli import reconcile_command  # noqa: E402

if __name__ == "__main__":
    reconcile_command(prog_name="reconcile_database.py")
