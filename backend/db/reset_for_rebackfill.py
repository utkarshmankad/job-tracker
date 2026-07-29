"""
Reset the database for a clean re-backfill.

Deletes all applications, status history, and processed-message records, then
resets the poller's last_history_id to NULL so the next poll fetches the full
BACKFILL_DAYS window with the updated parser.

Run from the repo root:
    python -m backend.db.reset_for_rebackfill
"""

import sys
from pathlib import Path

import structlog

from backend.config import DB_PATH
from backend.db.data_store import DataStore

log = structlog.get_logger(__name__)


def reset(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        print(f"DB not found at {db_path}")
        sys.exit(1)

    ds = DataStore(db_path)
    n_apps, n_proc = ds.count_applications_and_processed()

    print(f"This will delete {n_apps} application(s) and {n_proc} processed-message record(s).")
    print("The next poll will re-backfill everything from scratch.")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    n_apps, n_proc = ds.reset_for_rebackfill()
    log.info("db_reset_for_rebackfill", applications_deleted=n_apps, processed_messages_deleted=n_proc)

    print(f"Deleted {n_apps} application(s) and {n_proc} processed-message record(s).")
    print("Done. Start the backend — the poller will re-backfill on the next poll cycle.")


if __name__ == "__main__":
    reset()
