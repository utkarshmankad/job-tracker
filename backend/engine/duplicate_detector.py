"""Fuzzy duplicate detection for job applications."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog
from rapidfuzz import fuzz

from backend.config import DUPLICATE_FUZZY_THRESHOLD
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, utc_now
from backend.parser.email_parser import ParsedApplication

log = structlog.get_logger(__name__)

_LOOKUP_DAYS = 180


def _normalize_text(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return re.sub(
        r"\b(pvt|private|limited|ltd|inc|llc|technologies|technology)\b", "", text
    ).strip()


def _canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip().lower()
    kept = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(kept), "")
    )


class DuplicateDetector:
    def __init__(self, db: DataStore, threshold: int = DUPLICATE_FUZZY_THRESHOLD) -> None:
        self._db = db
        self._threshold = threshold

    def find_duplicate(self, parsed: ParsedApplication) -> Application | None:
        cutoff = utc_now() - timedelta(days=_LOOKUP_DAYS)
        candidates, _ = self._db.get_applications(
            ApplicationFilter(date_from=cutoff, page_size=10_000)
        )

        company = _normalize_text(parsed.company)
        role = _normalize_text(parsed.role)
        query = f"{company} {role}".strip()

        parsed_url = _canonical_url(parsed.job_url)
        if parsed_url:
            exact_url = next(
                (app for app in candidates if _canonical_url(app.job_url) == parsed_url), None
            )
            if exact_url is not None:
                return exact_url

        same_company = [
            app for app in candidates if company and _normalize_text(app.company) == company
        ]
        if parsed.status_signal is not None and len(same_company) == 1:
            # Status/interview mail often omits the original role and starts a new Gmail
            # thread. A single recent application at that company is the safest anchor.
            return same_company[0]

        if not query or not candidates:
            return None

        best_score = 0.0
        best_match: Application | None = None

        for app in candidates:
            target = f"{_normalize_text(app.company)} {_normalize_text(app.role)}".strip()
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

    def find_candidate_pairs(self) -> list[dict]:
        """Return high-confidence duplicate pairs for review; never mutates records."""
        apps, _ = self._db.get_applications(ApplicationFilter(page_size=10_000))
        pairs: list[dict] = []
        for index, left in enumerate(apps):
            left_url = _canonical_url(left.job_url)
            left_company = _normalize_text(left.company)
            left_role = _normalize_text(left.role)
            for right in apps[index + 1 :]:
                reasons: list[str] = []
                score = 0.0
                right_url = _canonical_url(right.job_url)
                right_company = _normalize_text(right.company)
                right_role = _normalize_text(right.role)
                if left_url and left_url == right_url:
                    score = 100.0
                    reasons.append("Same canonical job URL")
                elif left_company and left_role and right_company and right_role:
                    score = fuzz.ratio(
                        f"{left_company} {left_role}", f"{right_company} {right_role}"
                    )
                    if left_company == right_company:
                        reasons.append("Same normalized company")
                    if left_role == right_role:
                        reasons.append("Same normalized role")
                if score >= self._threshold:
                    pairs.append(
                        {
                            "primary": left,
                            "duplicate": right,
                            "score": round(score, 1),
                            "reasons": reasons or ["Similar company and role"],
                        }
                    )
        return sorted(pairs, key=lambda pair: pair["score"], reverse=True)

    def merge(self, existing: Application, parsed: ParsedApplication) -> Application:
        thread_ids: list[str] = json.loads(existing.thread_ids or "[]")
        if parsed.thread_id not in thread_ids:
            thread_ids.append(parsed.thread_id)
        existing.thread_ids = json.dumps(thread_ids)

        if parsed.applied_date < existing.applied_date:
            existing.applied_date = parsed.applied_date

        return self._db.upsert_application(existing)
