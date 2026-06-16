"""
One-off import: populate the DB from Job_Application_Tracker.xlsx.

Run from repo root:
    python -m backend.db.import_from_excel /path/to/Job_Application_Tracker.xlsx
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

from backend.config import DB_PATH

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


def _applied_date(val) -> str:
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S.000000")
    if isinstance(val, str):
        try:
            return datetime.strptime(val, "%Y-%m-%d").strftime("%Y-%m-%d %H:%M:%S.000000")
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

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM application")
    existing = c.fetchone()[0]
    print(f"\nExisting DB records: {existing}")
    print("Clearing application, statushistory, processedmessage tables…")

    c.execute("DELETE FROM statushistory")
    c.execute("DELETE FROM application")
    c.execute("DELETE FROM processedmessage")
    c.execute("UPDATE pollerstate SET last_history_id = NULL WHERE id = 1")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000000")

    for r in rows:
        c.execute(
            """
            INSERT INTO application
                (company, role, source_portal, job_url, applied_date,
                 current_status, thread_ids, is_false_positive, created_at, updated_at)
            VALUES (?, ?, ?, NULL, ?, ?, '[]', 0, ?, ?)
            """,
            (
                r["company"],
                r["role"],
                r["source_portal"],
                r["applied_date"],
                r["current_status"],
                now,
                now,
            ),
        )
        app_id = c.lastrowid
        c.execute(
            """
            INSERT INTO statushistory
                (application_id, from_status, to_status, trigger, changed_at, message_id)
            VALUES (?, NULL, ?, 'manual', ?, NULL)
            """,
            (app_id, r["current_status"], r["applied_date"]),
        )

    conn.commit()
    conn.close()

    print(f"\nDone — inserted {len(rows)} applications.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m backend.db.import_from_excel <path/to/tracker.xlsx>")
        sys.exit(1)
    run(Path(sys.argv[1]))
