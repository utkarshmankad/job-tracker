"""
One-off import: populate the DB from Job_Application_Tracker.xlsx.

Run from repo root:
    python -m backend.db.import_from_excel /path/to/Job_Application_Tracker.xlsx
"""

import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import structlog

from backend.config import DB_PATH
from backend.db.data_store import DataStore

log = structlog.get_logger(__name__)

# Row 5 onward in the sheet is real data (rows 1-4 are title / meta / blank / headers)
DATA_START_ROW = 5

STATUS_MAP = {
    "Applied": "Applied",
    "Rejected": "Rejected",
    "Interview in Progress": "Interview In Progress",
    "Interview Scheduled": "Interview Scheduled",
    "Resume Shortlisted": "Resume Shortlisted",
    "Offer Negotiation": "Offer Negotiation",
    "Offer": "Offer",
    "Joined": "Joined",
    "Withdrawn": "Withdrawn",
}


def _status(raw: str | None) -> str:
    if not raw:
        return "Applied"
    mapped = STATUS_MAP.get(raw.strip())
    if mapped is None:
        print(f"  WARNING: unknown status {raw!r} — defaulting to 'Applied'")
        return "Applied"
    return mapped


def _portal(raw: str | None) -> str:
    if not raw:
        return "Direct/Consultancy"
    return raw.strip()


def _applied_date(val) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.strptime(val, "%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"Cannot parse applied date: {val!r}")


def run(xlsx_path: Path, db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        sys.exit(1)

    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Tracker"]

    rows = []
    for row in ws.iter_rows(min_row=DATA_START_ROW, values_only=True):
        seq, company, position, _job_id, portal, applied_on, _hr_email, current_status, *_ = row
        if seq is None:
            continue  # trailing empty rows
        rows.append(
            {
                "company": str(company).strip() if company else None,
                "role": str(position).strip() if position else None,
                "source_portal": _portal(portal),
                "applied_date": _applied_date(applied_on),
                "current_status": _status(current_status),
            }
        )

    print(f"Excel rows to import: {len(rows)}")
    print("Preview (first 5):")
    for r in rows[:5]:
        print(" ", r)

    ds = DataStore(db_path)
    existing, _ = ds.count_applications_and_processed()
    print(f"\nExisting DB records: {existing}")
    print("Clearing application, statushistory, processedmessage tables…")

    now = datetime.utcnow()
    inserted = ds.bulk_import_applications(rows, now)
    log.info("excel_import_complete", rows_inserted=inserted)

    print(f"\nDone — inserted {inserted} applications.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m backend.db.import_from_excel <path/to/tracker.xlsx>")
        sys.exit(1)
    run(Path(sys.argv[1]))
