"""Fuzzy duplicate detection for job applications."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import structlog
from rapidfuzz import fuzz

from backend.config import DUPLICATE_FUZZY_THRESHOLD
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, utc_now
from backend.parser.email_parser import ParsedApplication

log = structlog.get_logger(__name__)

_LOOKUP_DAYS = 90


class DuplicateDetector:
    def __init__(self, db: DataStore, threshold: int = DUPLICATE_FUZZY_THRESHOLD) -> None:
        self._db = db
        self._threshold = threshold

    def find_duplicate(self, parsed: ParsedApplication) -> Application | None:
        cutoff = utc_now() - timedelta(days=_LOOKUP_DAYS)
        candidates, _ = self._db.get_applications(
            ApplicationFilter(
                date_from=cutoff,
                source_portal=parsed.source_portal,
                page_size=10_000,
            )
        )

        company = (parsed.company or "").strip()
        role = (parsed.role or "").strip()
        query = f"{company} {role}".strip()

        if not query or not candidates:
            return None

        best_score = 0
        best_match: Application | None = None

        for app in candidates:
            target = f"{app.company or ''} {app.role or ''}".strip()
            if not target:
                continue
            score = fuzz.ratio(query, target)
            if score > best_score:
                best_score = score
                best_match = app

        if best_match is not None and best_score >= self._threshold:
            log.info(
                "duplicate_found",
                score=best_score,
                application_id=best_match.id,
                query=query,
            )
            return best_match

        return None

    def merge(self, existing: Application, parsed: ParsedApplication) -> Application:
        thread_ids: list[str] = json.loads(existing.thread_ids or "[]")
        if parsed.thread_id not in thread_ids:
            thread_ids.append(parsed.thread_id)
        existing.thread_ids = json.dumps(thread_ids)

        if parsed.applied_date < existing.applied_date:
            existing.applied_date = parsed.applied_date

        return self._db.upsert_application(existing)
