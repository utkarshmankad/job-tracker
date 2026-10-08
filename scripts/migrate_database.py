#!/usr/bin/env python3
"""Show or apply Alembic schema migrations (backup and verify first).

Usage: python scripts/migrate_database.py --help   (see docs/database-operations.md)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.recovery_cli import migrate_group  # noqa: E402

if __name__ == "__main__":
    migrate_group(prog_name="migrate_database.py")
