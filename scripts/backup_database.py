#!/usr/bin/env python3
"""Create a verified online backup of the Job Tracker SQLite database.

Usage: python scripts/backup_database.py --help   (see docs/database-operations.md)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.recovery_cli import backup_command  # noqa: E402

if __name__ == "__main__":
    backup_command(prog_name="backup_database.py")
