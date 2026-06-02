"""Import job applications from a LinkedIn CSV export.

LinkedIn does not expose a public API for the Jobs Tracker. The two
legitimate extraction channels are:
  1. Settings → Data Privacy → Get a copy of your data → Jobs (CSV)
  2. The direct Export button on linkedin.com/jobs-tracker/

Both produce structurally similar CSVs but column names have drifted
across LinkedIn revisions; _COLUMN_ALIASES normalises them all.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import structlog

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus

log = structlog.get_logger(__name__)

SOURCE_PORTAL = "LinkedIn"

# ── Column name normalisation ────────────────────────────────────────────────
# Keys are lowercase-stripped raw header text; values are canonical field names.
_COLUMN_ALIASES: dict[str, str] = {
    # Company
    "company name": "company",
    "company": "company",
    "employer": "company",
    "organization": "company",
    # Role
    "title": "role",
    "job title": "role",
    "position": "role",
    "role": "role",
    "job name": "role",
    # Job URL
    "url": "job_url",
    "job url": "job_url",
    "link": "job_url",
    "job link": "job_url",
    "application url": "job_url",
    "job posting url": "job_url",
    # Applied date
    "date applied": "applied_date",
    "applied at": "applied_date",
    "applied date": "applied_date",
    "application date": "applied_date",
    "date saved": "applied_date",
    "saved at": "applied_date",
    "started on": "applied_date",
    # Status
    "status": "linkedin_status",
    "application status": "linkedin_status",
    "current status": "linkedin_status",
    "stage": "linkedin_status",
}

# ── LinkedIn status → our ApplicationStatus ──────────────────────────────────
_STATUS_MAP: dict[str, ApplicationStatus] = {
    "saved": ApplicationStatus.APPLIED,
    "applied": ApplicationStatus.APPLIED,
    "submitted": ApplicationStatus.APPLIED,
    "in review": ApplicationStatus.RESUME_SHORTLISTED,
    "application viewed": ApplicationStatus.RESUME_SHORTLISTED,
    "viewed": ApplicationStatus.RESUME_SHORTLISTED,
    "shortlisted": ApplicationStatus.RESUME_SHORTLISTED,
    "resume shortlisted": ApplicationStatus.RESUME_SHORTLISTED,
    "interviewing": ApplicationStatus.INTERVIEW_SCHEDULED,
    "interview": ApplicationStatus.INTERVIEW_SCHEDULED,
    "interview scheduled": ApplicationStatus.INTERVIEW_SCHEDULED,
    "phone screen": ApplicationStatus.INTERVIEW_SCHEDULED,
    "phone interview": ApplicationStatus.INTERVIEW_SCHEDULED,
    "interview in progress": ApplicationStatus.INTERVIEW_IN_PROGRESS,
    "assessment": ApplicationStatus.INTERVIEW_IN_PROGRESS,
    "offer": ApplicationStatus.OFFER,
    "offered": ApplicationStatus.OFFER,
    "job offer": ApplicationStatus.OFFER,
    "offer received": ApplicationStatus.OFFER,
    "offer extended": ApplicationStatus.OFFER,
    "negotiating": ApplicationStatus.OFFER_NEGOTIATION,
    "offer negotiation": ApplicationStatus.OFFER_NEGOTIATION,
    "rejected": ApplicationStatus.REJECTED,
    "not selected": ApplicationStatus.REJECTED,
    "declined by company": ApplicationStatus.REJECTED,
    "unsuccessful": ApplicationStatus.REJECTED,
    "position filled": ApplicationStatus.REJECTED,
    "no longer considered": ApplicationStatus.REJECTED,
    "withdrawn": ApplicationStatus.WITHDRAWN,
    "withdrew application": ApplicationStatus.WITHDRAWN,
    "application withdrawn": ApplicationStatus.WITHDRAWN,
    "cancelled": ApplicationStatus.WITHDRAWN,
}

# Numeric rank for comparing how far along a status is.
# Used to decide whether a LinkedIn status represents an advancement.
_STATUS_RANK: dict[ApplicationStatus, int] = {
    ApplicationStatus.APPLIED: 0,
    ApplicationStatus.RESUME_SHORTLISTED: 1,
    ApplicationStatus.INTERVIEW_SCHEDULED: 2,
    ApplicationStatus.INTERVIEW_IN_PROGRESS: 3,
    ApplicationStatus.OFFER_NEGOTIATION: 4,
    ApplicationStatus.OFFER: 5,
    ApplicationStatus.JOINED: 6,
    ApplicationStatus.REJECTED: 7,
    ApplicationStatus.WITHDRAWN: 8,
}

# Date formats LinkedIn has used in exports over time.
_DATE_FMTS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%B %d, %Y",  # January 15, 2024
    "%b %d, %Y",  # Jan 15, 2024
    "%B %d %Y",   # January 15 2024 (no comma)
    "%b %d %Y",   # Jan 15 2024 (no comma)
    "%d %B %Y",   # 15 January 2024
    "%d %b %Y",   # 15 Jan 2024
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%m-%d-%Y",
]


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ParsedRow:
    company: Optional[str]
    role: Optional[str]
    job_url: Optional[str]
    applied_date: datetime
    status: ApplicationStatus
    date_defaulted: bool  # True if date could not be parsed and today was used
    raw: dict  # original CSV row kept for debugging


@dataclass
class RowResult:
    action: str  # "created" | "updated" | "skipped" | "error"
    company: Optional[str]
    role: Optional[str]
    status: str
    detail: str = ""


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    warnings: list[str] = field(default_factory=list)
    rows: list[RowResult] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalise_column_map(headers: list[str]) -> dict[str, str]:
    """Return {canonical_field: original_header} for each recognised column."""
    mapping: dict[str, str] = {}
    for h in headers:
        canonical = _COLUMN_ALIASES.get(h.strip().lower())
        if canonical and canonical not in mapping:
            mapping[canonical] = h
    return mapping


def _map_status(raw: str) -> ApplicationStatus:
    return _STATUS_MAP.get(raw.strip().lower(), ApplicationStatus.APPLIED)


def _parse_date(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# ── Importer ─────────────────────────────────────────────────────────────────

class LinkedInImporter:
    """Parse a LinkedIn CSV export and write rows into DataStore.

    Typical usage::

        importer = LinkedInImporter()
        rows, warnings = importer.parse_csv(file_bytes)
        result = importer.import_to_db(db, rows)
    """

    def parse_csv(self, content: bytes) -> tuple[list[ParsedRow], list[str]]:
        """Decode and parse CSV bytes into ParsedRow objects.

        Returns (rows, warnings). Warnings describe recoverable issues such as
        unrecognised date formats or missing optional columns.
        """
        # Decode with BOM tolerance (Excel-generated CSVs prepend a BOM).
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers: list[str] = list(reader.fieldnames or [])

        col_map = _normalise_column_map(headers)
        warnings: list[str] = []

        if not col_map:
            warnings.append(
                "No recognised columns found. "
                f"LinkedIn export headers were: {headers}. "
                "Expected names like 'Company Name', 'Title', 'Date Applied', 'Status'."
            )
            return [], warnings

        if "company" not in col_map and "role" not in col_map:
            warnings.append(
                "Neither a company nor a role column was detected. "
                "Rows will be imported but may be hard to identify."
            )

        rows: list[ParsedRow] = []
        for line_no, csv_row in enumerate(reader, start=2):
            company = (csv_row.get(col_map.get("company", "")) or "").strip() or None
            role = (csv_row.get(col_map.get("role", "")) or "").strip() or None

            url_raw = (csv_row.get(col_map.get("job_url", "")) or "").strip()
            job_url = url_raw if url_raw.startswith("http") else None

            date_raw = (csv_row.get(col_map.get("applied_date", "")) or "").strip()
            parsed_date = _parse_date(date_raw) if date_raw else None
            date_defaulted = False
            if not parsed_date:
                if date_raw:
                    warnings.append(
                        f"Row {line_no}: unrecognised date format '{date_raw}' — using today"
                    )
                applied_date = datetime.utcnow()
                date_defaulted = True
            else:
                applied_date = parsed_date

            status_raw = (csv_row.get(col_map.get("linkedin_status", "")) or "Applied").strip()
            status = _map_status(status_raw)

            rows.append(ParsedRow(
                company=company,
                role=role,
                job_url=job_url,
                applied_date=applied_date,
                status=status,
                date_defaulted=date_defaulted,
                raw=dict(csv_row),
            ))

        return rows, warnings

    def import_to_db(self, db: DataStore, rows: list[ParsedRow]) -> ImportResult:
        """Write parsed rows into DataStore, deduplicating against existing data."""
        result = ImportResult()
        for row in rows:
            try:
                outcome = self._process_row(db, row)
                result.rows.append(outcome)
                if outcome.action == "created":
                    result.created += 1
                elif outcome.action == "updated":
                    result.updated += 1
                elif outcome.action == "skipped":
                    result.skipped += 1
                else:
                    result.errors += 1
            except Exception as exc:
                log.warning(
                    "linkedin_import.row_error",
                    company=row.company,
                    role=row.role,
                    error=str(exc),
                )
                result.errors += 1
                result.rows.append(RowResult(
                    action="error",
                    company=row.company,
                    role=row.role,
                    status=row.status.value,
                    detail=str(exc),
                ))
        return result

    # ── Private ───────────────────────────────────────────────────────────

    def _process_row(self, db: DataStore, row: ParsedRow) -> RowResult:
        existing = self._find_duplicate(db, row)

        if existing is not None:
            existing_rank = _STATUS_RANK.get(existing.current_status, 0)
            new_rank = _STATUS_RANK.get(row.status, 0)

            if new_rank > existing_rank:
                old_status = existing.current_status.value
                existing.current_status = row.status
                existing.updated_at = datetime.utcnow()
                db.upsert_application(existing)
                assert existing.id is not None
                db.append_status_history(
                    application_id=existing.id,
                    from_status=old_status,
                    to_status=row.status.value,
                    trigger="linkedin_import",
                )
                return RowResult(
                    action="updated",
                    company=row.company,
                    role=row.role,
                    status=row.status.value,
                    detail=f"{old_status} → {row.status.value}",
                )

            return RowResult(
                action="skipped",
                company=row.company,
                role=row.role,
                status=row.status.value,
                detail="Already tracked; LinkedIn status is not more advanced",
            )

        # No duplicate found — create a new application.
        app = Application(
            company=row.company,
            role=row.role,
            source_portal=SOURCE_PORTAL,
            job_url=row.job_url,
            applied_date=row.applied_date,
            current_status=row.status,
        )
        app = db.upsert_application(app)
        assert app.id is not None
        db.append_status_history(
            application_id=app.id,
            from_status=None,
            to_status=row.status.value,
            trigger="linkedin_import",
        )
        return RowResult(
            action="created",
            company=row.company,
            role=row.role,
            status=row.status.value,
            detail="New application created from LinkedIn export",
        )

    def _find_duplicate(self, db: DataStore, row: ParsedRow) -> Application | None:
        """Two-probe duplicate detection.

        1. Exact job URL match — zero false positives.
        2. Company + role + ±7-day applied-date window — catches apps the Gmail
           poller already found before the user imported from LinkedIn.
        """
        if row.job_url:
            match = db.find_application_by_url(row.job_url)
            if match:
                return match

        if row.company and row.role and not row.date_defaulted:
            match = db.find_similar_application(
                company=row.company,
                role=row.role,
                applied_date=row.applied_date,
                window_days=7,
            )
            if match:
                return match

        return None
