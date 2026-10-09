"""Fuzzy duplicate detection for job applications."""

from __future__ import annotations

import json

import structlog

from backend.config import DUPLICATE_SUGGESTION_SCORE
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application
from backend.engine.identity_resolver import compare_applications
from backend.parser.email_parser import ParsedApplication

log = structlog.get_logger(__name__)

_LOOKUP_DAYS = 180


class DuplicateDetector:
    def __init__(self, db: DataStore, threshold: int = DUPLICATE_SUGGESTION_SCORE) -> None:
        self._db = db
        self._threshold = threshold

    def find_candidate_pairs(self) -> list[dict]:
        """Possible duplicate pairs for human review; never mutates records.

        Uses the identity resolver's pairwise comparison, so suggestions and evidence
        resolution share one set of identifiers, normalization rules and weights.
        """
        apps, _ = self._db.get_applications(ApplicationFilter(page_size=10_000))
        apps = sorted(apps, key=lambda a: a.id or 0)
        pairs: list[dict] = []
        for index, left in enumerate(apps):
            for right in apps[index + 1 :]:
                score, reasons = compare_applications(left, right)
                if score >= self._threshold:
                    pairs.append(
                        {
                            "primary": left,
                            "duplicate": right,
                            "score": float(score),
                            "reasons": reasons or ["Similar company and role"],
                        }
                    )
        return sorted(pairs, key=lambda pair: (-pair["score"], pair["primary"].id or 0))

    def merge(self, existing: Application, parsed: ParsedApplication) -> Application:
        thread_ids: list[str] = json.loads(existing.thread_ids or "[]")
        if parsed.thread_id not in thread_ids:
            thread_ids.append(parsed.thread_id)
        existing.thread_ids = json.dumps(thread_ids)

        if parsed.applied_date < existing.applied_date:
            existing.applied_date = parsed.applied_date

        return self._db.upsert_application(existing)
