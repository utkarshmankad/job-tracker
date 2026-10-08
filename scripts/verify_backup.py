#!/usr/bin/env python3
"""Verify a backup by restoring it into a temporary directory and opening it.

Usage: python scripts/verify_backup.py --help   (see docs/database-operations.md)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.recovery_cli import verify_command  # noqa: E402

if __name__ == "__main__":
    verify_command(prog_name="verify_backup.py")
