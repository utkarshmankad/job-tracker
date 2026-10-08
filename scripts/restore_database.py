#!/usr/bin/env python3
"""Validate a backup and restore it to an explicit destination.

Usage: python scripts/restore_database.py --help   (see docs/database-operations.md)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.recovery_cli import restore_command  # noqa: E402

if __name__ == "__main__":
    restore_command(prog_name="restore_database.py")
