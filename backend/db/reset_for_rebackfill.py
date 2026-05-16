"""
Reset the database for a clean re-backfill.

Deletes all applications, status history, and processed-message records, then
resets the poller's last_history_id to NULL so the next poll fetches the full
BACKFILL_DAYS window with the updated parser.

Run from the repo root:
    python -m backend.db.reset_for_rebackfill
"""

import sys
import sqlite3
from pathlib import Path

from backend.config import DB_PATH


def reset(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        print(f"DB not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM application")
    n_apps = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM processedmessage")
    n_proc = c.fetchone()[0]

    print(f"This will delete {n_apps} application(s) and {n_proc} processed-message record(s).")
    print("The next poll will re-backfill everything from scratch.")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    c.execute("DELETE FROM statushistory")
    c.execute("DELETE FROM application")
    c.execute("DELETE FROM processedmessage")
    c.execute("UPDATE pollerstate SET last_history_id = NULL WHERE id = 1")
    conn.commit()
    conn.close()

    print("Done. Start the backend — the poller will re-backfill on the next poll cycle.")


if __name__ == "__main__":
    reset()
