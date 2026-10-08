"""Decide which application a job-related email belongs to — or that nobody can tell yet.

Rules (docs/phase-2-identity-resolution.md §3), first decisive rule wins:

1. Gmail thread already mapped to an application            → thread        1.0
2. canonical job URL matches exactly one application         → job_url       0.95
3. status-bearing mail, exactly one application at company   → company_only  0.6
4. fuzzy "company role" ≥ threshold, single best candidate   → company_role  score/100
5. status-bearing mail, several applications at the company  → ambiguous

Rules 2–4 keep the Phase 1 DuplicateDetector.find_duplicate priority and text
normalization, so existing matching does not drift; the ambiguity outcomes are new.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import timedelta

from rapidfuzz import fuzz

from backend.config import DUPLICATE_FUZZY_THRESHOLD, IDENTITY_LOOKBACK_DAYS
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, LinkMethod, utc_now
from backend.engine.normalization import canonical_job_url, normalize_company
from backend.parser.email_parser import ParsedApplication


class MessageKind(enum.StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"  # "we received your application" — may create one
    STATUS_UPDATE = "status_update"  # carries a status signal (shortlist, rejection, offer…)
    FOLLOW_UP = "follow_up"  # replies, scheduling, reminders, assessments, feedback…


# Vocabulary of mail that follows an application rather than acknowledging a new one.
_FOLLOW_UP_PATTERN = re.compile(
    r"^\s*(re|fw|fwd)\s*:"
    r"|\binterview"
    r"|\bschedul"
    r"|\bavailability\b"
    r"|\bassessment"
    r"|\bassignment\b"
    r"|\b(coding|online|technical) (test|challenge|round)"
    r"|\breminder\b"
    r"|\bfollow[\s-]?up\b"
    r"|\bnext steps?\b"
    r"|\bfeedback\b"
    r"|\bupdate on your application\b"
    r"|\bapplication (status|update)\b"
    r"|\binvitation\b"
    r"|\binvite\b"
    r"|\boffer\b"
    r"|\bcalendar\b",
    re.IGNORECASE,
)


def classify_message_kind(parsed: ParsedApplication) -> MessageKind:
    if parsed.status_signal is not None:
        return MessageKind.STATUS_UPDATE
    if _FOLLOW_UP_PATTERN.search(parsed.raw_subject or ""):
        return MessageKind.FOLLOW_UP
    return MessageKind.ACKNOWLEDGEMENT


@dataclass(frozen=True)
class IdentityMatch:
    application: Application | None = None
    method: str | None = None
    confidence: float | None = None
    ambiguity: str | None = None  # set when several applications fit and none is decisive


def _company_key(app: Application) -> str:
    return app.normalized_company or normalize_company(app.company)


def _fuzzy_key(company: str | None, role: str | None) -> str:
    # Same text normalization the Phase 1 detector applied to both company and role.
    return f"{normalize_company(company)} {normalize_company(role)}".strip()


class IdentityResolver:
    def __init__(
        self,
        db: DataStore,
        threshold: int = DUPLICATE_FUZZY_THRESHOLD,
        lookback_days: int = IDENTITY_LOOKBACK_DAYS,
    ) -> None:
        self._db = db
        self._threshold = threshold
        self._lookback_days = lookback_days

    def resolve(self, parsed: ParsedApplication) -> IdentityMatch:
        by_thread = self._db.find_application_by_thread_id(parsed.thread_id)
        if by_thread is not None:
            return IdentityMatch(by_thread, LinkMethod.THREAD.value, 1.0)

        cutoff = utc_now() - timedelta(days=self._lookback_days)
        candidates, _ = self._db.get_applications(
            ApplicationFilter(date_from=cutoff, page_size=10_000)
        )

        url = canonical_job_url(parsed.job_url)
        if url:
            same_url = [
                a
                for a in candidates
                if (a.canonical_job_url or canonical_job_url(a.job_url)) == url
            ]
            if len(same_url) == 1:
                return IdentityMatch(same_url[0], LinkMethod.JOB_URL.value, 0.95)
            if len(same_url) > 1:
                return IdentityMatch(ambiguity="ambiguous_job_url")

        company = normalize_company(parsed.company)
        same_company = [a for a in candidates if company and _company_key(a) == company]
        if parsed.status_signal is not None and len(same_company) == 1:
            # Status mail often omits the role and starts a new thread; a single application
            # at that company is the safest anchor (Phase 1 behaviour).
            return IdentityMatch(same_company[0], LinkMethod.COMPANY_ONLY.value, 0.6)

        query = _fuzzy_key(parsed.company, parsed.role)
        if query:
            scored = [
                (fuzz.ratio(query, target), app)
                for app in candidates
                if (target := _fuzzy_key(app.company, app.role))
            ]
            passing = [(score, app) for score, app in scored if score >= self._threshold]
            if passing:
                best = max(score for score, _ in passing)
                top = {app.id: app for score, app in passing if score == best}
                if len(top) == 1:
                    winner = next(iter(top.values()))
                    return IdentityMatch(
                        winner, LinkMethod.COMPANY_ROLE.value, round(best / 100, 3)
                    )
                return IdentityMatch(ambiguity="ambiguous_company_role")

        if parsed.status_signal is not None and len(same_company) > 1:
            return IdentityMatch(ambiguity="ambiguous_company")
        return IdentityMatch()
