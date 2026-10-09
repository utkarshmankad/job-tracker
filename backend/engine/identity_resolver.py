"""Deterministic, explainable resolution of evidence to applications (resolver v2).

Pipeline (docs/phase-2-identity-resolution.md §11):

1. **Signals** — normalized identifiers extracted from the evidence (thread, external job ID,
   canonical job URL, personal sender address, employer domain, company, role, source, date,
   message kind, forwarded flag).
2. **Candidates** — a bounded set from indexed lookups, strongest first: same external job
   ID, same canonical URL, Gmail thread already mapped, applications this sender's or
   domain's evidence is linked to, same normalized company, same first company word.
   Never a full-table fuzzy scan.
3. **Scoring** — fixed integer weights from ``backend.config.RESOLVER_WEIGHTS``; every
   candidate keeps the list of signals that produced its score.
4. **Decision** — conflicting strong identifiers force review; auto-link needs a high score
   *and* a clear lead; a plausible candidate blocks "new application"; a new application
   requires a credible acknowledgement. The result records version, outcome, confidence,
   reason, explanation and the candidate list.

No LLM, no randomness, no wall-clock dependence beyond the evidence's own dates; candidate
order ties break on application ID, so results are identical across runs and processes.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

from backend import config as app_config
from backend.db.models import Application, ApplicationStatus, LinkMethod
from backend.engine.normalization import (
    canonical_job_url,
    company_domain,
    external_job_id_from_url,
    is_forwarded_subject,
    is_freemail_domain,
    normalize_company,
    normalize_external_job_id,
    normalize_role,
    normalize_source,
    normalize_thread_id,
    personal_sender_address,
    sender_domain,
)

if TYPE_CHECKING:
    from backend.db.data_store import DataStore
    from backend.db.models import Evidence
    from backend.parser.email_parser import ParsedApplication

RESOLVER_VERSION = "2.0.0"

_TERMINAL = {ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN, ApplicationStatus.JOINED}
_STRONG_SIGNALS = frozenset(
    {
        "same_gmail_thread",
        "same_external_job_id",
        "same_canonical_job_url",
        "sender_linked_by_human",
    }
)
_COMPANY_LIMIT = 50
_PREFIX_LIMIT = 25
_SENDER_LIMIT = 25


class MessageKind(enum.StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"  # "we received your application" — may create one
    STATUS_UPDATE = "status_update"  # carries a status signal (shortlist, rejection, offer…)
    FOLLOW_UP = "follow_up"  # replies, scheduling, reminders, assessments, feedback…


class Outcome(enum.StrEnum):
    LINKED = "linked"
    NEW_APPLICATION = "new_application"
    REVIEW_REQUIRED = "review_required"
    IGNORED = "ignored"


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

_REASON_TEXT = {
    "auto_link": "One application clearly matches",
    "conflicting_strong_identifiers": "Strong identifiers point at different applications",
    "strong_identifier_conflict": "A strong identifier matches but another contradicts it",
    "strong_identifier_below_threshold": "A strong identifier matches but other signals disagree",
    "strong_identifier_disagrees_with_text": (
        "An exact identifier names one application but company/role match another"
    ),
    "ambiguous_candidates": "Several applications match about equally well",
    "below_auto_link_threshold": "A plausible application matches, but not confidently enough",
    "possible_reapplication": (
        "Same company and role as an earlier application outside the date window"
    ),
    "credible_acknowledgement": "Application acknowledgement with no matching application",
    "insufficient_identity": (
        "No company, role, job link or known sender to identify an application"
    ),
    "follow_up_without_application": "Follow-up message with no matching application",
    "status_update_without_application": "Status update with no matching application",
    "not_a_credible_new_application": (
        "Looks like an acknowledgement but lacks a credible sender or company"
    ),
}


def classify_message_kind(parsed: ParsedApplication) -> MessageKind:
    if parsed.status_signal is not None:
        return MessageKind.STATUS_UPDATE
    if _FOLLOW_UP_PATTERN.search(parsed.raw_subject or ""):
        return MessageKind.FOLLOW_UP
    return MessageKind.ACKNOWLEDGEMENT


# ------------------------------------------------------------------ #
# Signals                                                              #
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class EvidenceSignals:
    occurred_at: datetime
    kind: MessageKind
    thread_id: str | None = None
    external_job_id: str | None = None
    external_job_source: str | None = None
    canonical_url: str | None = None
    sender_address: str | None = None  # personal addresses only
    company_domain: str | None = None  # employer domain (never vendor/free-mail)
    sender_is_freemail: bool = False
    company: str = ""
    role: str = ""
    source: str = "other"  # canonical portal/source key
    status_signal: ApplicationStatus | None = None
    forwarded: bool = False
    classification_confident: bool = True

    @property
    def has_identity(self) -> bool:
        return bool(
            self.company
            or self.role
            or self.canonical_url
            or self.external_job_id
            or self.sender_address
            or self.company_domain
            or self.thread_id
        )


def signals_from_parsed(parsed: ParsedApplication) -> EvidenceSignals:
    """Signals from a parsed email. Forwarded mail drops sender-based signals: the sender
    is whoever forwarded it, not the employer."""
    forwarded = is_forwarded_subject(parsed.raw_subject)
    url = canonical_job_url(parsed.job_url)
    extracted = external_job_id_from_url(url)
    domain = sender_domain(parsed.raw_sender)
    return EvidenceSignals(
        occurred_at=parsed.applied_date,
        kind=classify_message_kind(parsed),
        thread_id=normalize_thread_id(parsed.thread_id),
        external_job_id=extracted[1] if extracted else None,
        external_job_source=extracted[0] if extracted else None,
        canonical_url=url,
        sender_address=None if forwarded else personal_sender_address(parsed.raw_sender),
        company_domain=None if forwarded else company_domain(parsed.raw_sender),
        sender_is_freemail=is_freemail_domain(domain),
        company=normalize_company(parsed.company),
        role=normalize_role(parsed.role),
        source=normalize_source(parsed.source_portal),
        status_signal=parsed.status_signal,
        forwarded=forwarded,
        classification_confident=parsed.is_classification_confident,
    )


def signals_from_evidence(evidence: Evidence, kind: MessageKind) -> EvidenceSignals:
    """Signals for non-email evidence (portal imports, browser observations) or replays,
    built only from what the evidence row itself stores."""
    parser = (evidence.raw_metadata or {}).get("parser") or {}
    status_value = parser.get("status_signal") if isinstance(parser, dict) else None
    url = evidence.canonical_job_url or canonical_job_url(
        parser.get("job_url") if isinstance(parser, dict) else None
    )
    extracted = external_job_id_from_url(url)
    external = evidence.external_job_id or (extracted[1] if extracted else None)
    source = normalize_source(parser.get("portal") if isinstance(parser, dict) else evidence.source)
    return EvidenceSignals(
        occurred_at=evidence.occurred_at,
        kind=kind,
        thread_id=normalize_thread_id(evidence.thread_id),
        external_job_id=normalize_external_job_id(external),
        external_job_source=extracted[0] if extracted else source,
        canonical_url=url,
        sender_address=personal_sender_address(evidence.sender),
        company_domain=company_domain(evidence.sender),
        company=normalize_company(parser.get("company") if isinstance(parser, dict) else None),
        role=normalize_role(parser.get("role") if isinstance(parser, dict) else None),
        source=source,
        status_signal=ApplicationStatus(status_value) if status_value else None,
    )


# ------------------------------------------------------------------ #
# Results                                                              #
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class Signal:
    name: str
    weight: int


@dataclass(frozen=True)
class CandidateScore:
    application_id: int
    score: int
    signals: tuple[Signal, ...]

    @property
    def strong(self) -> bool:
        return any(s.name in _STRONG_SIGNALS for s in self.signals)

    def has(self, name: str) -> bool:
        return any(s.name == name for s in self.signals)

    def to_json(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "score": self.score,
            "signals": {s.name: s.weight for s in self.signals},
        }


@dataclass(frozen=True)
class ResolutionResult:
    outcome: Outcome
    reason: str
    confidence: float
    application_id: int | None = None
    candidates: tuple[CandidateScore, ...] = ()
    kind: MessageKind | None = None
    version: str = RESOLVER_VERSION
    link_method: str | None = None
    explanation: str = field(default="")

    @classmethod
    def ignored(cls, reason: str) -> ResolutionResult:
        return cls(Outcome.IGNORED, reason, 0.0, explanation=reason.replace("_", " ").capitalize())

    def to_json(self) -> dict[str, Any]:
        """Persisted with the evidence. Signal names, scores and IDs only — no message
        content, addresses or extracted text."""
        return {
            "version": self.version,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "explanation": self.explanation,
            "confidence": self.confidence,
            "selected_application_id": self.application_id,
            "link_method": self.link_method,
            "message_kind": self.kind.value if self.kind else None,
            "candidates": [c.to_json() for c in self.candidates[:5]],
            "thresholds": {
                "auto_link_score": app_config.RESOLVER_AUTO_LINK_SCORE,
                "auto_link_margin": app_config.RESOLVER_AUTO_LINK_MARGIN,
                "review_score": app_config.RESOLVER_REVIEW_SCORE,
            },
        }


# ------------------------------------------------------------------ #
# Scoring                                                              #
# ------------------------------------------------------------------ #


def _thread_ids(app: Application) -> set[str]:
    try:
        values = json.loads(app.thread_ids or "[]")
    except (json.JSONDecodeError, TypeError):
        return set()
    return {t for t in (normalize_thread_id(v) for v in values if isinstance(v, str)) if t}


def _app_company(app: Application) -> str:
    return app.normalized_company or normalize_company(app.company)


def _app_role(app: Application) -> str:
    return app.normalized_role or normalize_role(app.role)


def _app_url(app: Application) -> str | None:
    return app.canonical_job_url or canonical_job_url(app.job_url)


def _app_external(app: Application) -> tuple[str | None, str | None]:
    url = _app_url(app)
    extracted = external_job_id_from_url(url)
    job_id = normalize_external_job_id(app.external_job_id) or (extracted[1] if extracted else None)
    source = extracted[0] if extracted else normalize_source(app.source_portal)
    return job_id, source


def company_relation(left: str, right: str) -> str | None:
    """'exact', 'similar', 'conflict' or None (unknown/inconclusive)."""
    if not left or not right:
        return None
    if left == right:
        return "exact"
    lt, rt = left.split(), right.split()
    shorter, longer = (lt, rt) if len(lt) <= len(rt) else (rt, lt)
    if (
        longer[: len(shorter)] == shorter
        or fuzz.ratio(left, right) >= app_config.RESOLVER_COMPANY_SIMILARITY
    ):
        return "similar"
    if fuzz.ratio(left, right) < app_config.RESOLVER_COMPANY_CONFLICT_BELOW:
        return "conflict"
    return None


def role_relation(left: str, right: str) -> str | None:
    if not left or not right:
        return None
    if left == right:
        return "exact"
    score = fuzz.token_set_ratio(left, right)
    if score >= app_config.RESOLVER_ROLE_SIMILARITY:
        return "similar"
    if score < app_config.RESOLVER_ROLE_CONFLICT_BELOW:
        return "conflict"
    return None


def _in_date_window(occurred_at: datetime, applied: datetime) -> bool:
    delta = occurred_at - applied
    return (
        timedelta(days=-app_config.RESOLVER_DATE_WINDOW_BEFORE_DAYS)
        <= delta
        <= timedelta(days=app_config.RESOLVER_DATE_WINDOW_AFTER_DAYS)
    )


def score_candidate(
    signals: EvidenceSignals,
    app: Application,
    identity: dict[str, set[str]],
    sole_active_at_company: bool,
) -> CandidateScore:
    w = app_config.RESOLVER_WEIGHTS
    out: list[Signal] = []

    def add(name: str) -> None:
        out.append(Signal(name, w[name]))

    same_thread = bool(signals.thread_id and signals.thread_id in _thread_ids(app))
    if same_thread:
        add("same_gmail_thread")

    app_ext, app_ext_source = _app_external(app)
    if signals.external_job_id and app_ext:
        same_source = (signals.external_job_source or signals.source) == app_ext_source
        if signals.external_job_id == app_ext:
            add("same_external_job_id" if same_source else "same_external_job_id_other_source")
        elif same_source:
            add("external_job_id_conflict")

    app_url = _app_url(app)
    if signals.canonical_url and app_url:
        if signals.canonical_url == app_url:
            add("same_canonical_job_url")
        elif not (signals.external_job_id and signals.external_job_id == app_ext):
            add("job_url_differs")

    if signals.sender_address:
        if signals.sender_address in identity.get("senders_human", set()):
            add("sender_linked_by_human")
        elif signals.sender_address in identity.get("senders_resolver", set()):
            add("sender_linked_by_resolver")
    if signals.company_domain and signals.company_domain in identity.get("domains", set()):
        add("known_company_domain")

    company = company_relation(signals.company, _app_company(app))
    role = role_relation(signals.role, _app_role(app))
    if company == "exact":
        add("company_exact")
    elif company == "similar":
        add("company_similar")
    elif company == "conflict":
        add("text_mismatch_in_thread" if same_thread else "company_conflict")
    if role == "exact":
        add("role_exact")
    elif role == "similar":
        add("role_similar")
    elif role == "conflict":
        add("text_mismatch_in_thread" if same_thread else "role_conflict")

    if (
        sole_active_at_company
        and signals.kind in (MessageKind.STATUS_UPDATE, MessageKind.FOLLOW_UP)
        and role != "conflict"
    ):
        add("sole_active_application_at_company")

    add(
        "date_in_window"
        if _in_date_window(signals.occurred_at, app.applied_date)
        else "date_out_of_window"
    )
    if signals.source not in ("other", "gmail") and signals.source == normalize_source(
        app.source_portal
    ):
        add("source_match")
    if app.current_status in _TERMINAL and signals.kind is MessageKind.ACKNOWLEDGEMENT:
        add("terminal_application_new_acknowledgement")

    assert app.id is not None
    return CandidateScore(app.id, sum(s.weight for s in out), tuple(out))


def compare_applications(left: Application, right: Application) -> tuple[int, list[str]]:
    """Pairwise score and readable reasons for duplicate *suggestions*, using the same
    identifiers and weights as evidence resolution."""
    w = app_config.RESOLVER_WEIGHTS
    score = 0
    reasons: list[str] = []
    left_ext, left_source = _app_external(left)
    right_ext, right_source = _app_external(right)
    if left_ext and right_ext and left_source == right_source:
        if left_ext == right_ext:
            score += w["same_external_job_id"]
            reasons.append("Same job ID on the same source")
        else:
            score += w["external_job_id_conflict"]
            reasons.append("Different job IDs on the same source")
    left_url, right_url = _app_url(left), _app_url(right)
    if left_url and left_url == right_url:
        score += w["same_canonical_job_url"]
        reasons.append("Same canonical job URL")
    company = company_relation(_app_company(left), _app_company(right))
    role = role_relation(_app_role(left), _app_role(right))
    for relation, kind, label in ((company, "company", "company"), (role, "role", "role")):
        if relation == "exact":
            score += w[f"{kind}_exact"]
            reasons.append(f"Same normalized {label}")
        elif relation == "similar":
            score += w[f"{kind}_similar"]
            reasons.append(f"Similar {label}")
        elif relation == "conflict":
            score += w[f"{kind}_conflict"]
    gap = abs(left.applied_date - right.applied_date)
    if gap <= timedelta(days=app_config.RESOLVER_DATE_WINDOW_AFTER_DAYS):
        score += w["date_in_window"]
    else:
        score += w["date_out_of_window"]
        reasons.append("Applied far apart in time")
    return score, reasons


# ------------------------------------------------------------------ #
# Resolver                                                             #
# ------------------------------------------------------------------ #


class IdentityResolver:
    def __init__(self, db: DataStore) -> None:
        self._db = db

    # -- candidates -----------------------------------------------------

    def _candidates(self, signals: EvidenceSignals) -> list[Application]:
        ordered: dict[int, Application] = {}
        pending_ids: list[int] = []

        def add_apps(apps: list[Application]) -> None:
            for app in apps:
                if app.id is not None and app.id not in ordered:
                    ordered[app.id] = app

        if signals.external_job_id:
            add_apps(
                self._db.find_applications_by_external_job_id(
                    signals.external_job_id, _SENDER_LIMIT
                )
            )
        if signals.canonical_url:
            add_apps(
                self._db.find_applications_by_canonical_url(signals.canonical_url, _SENDER_LIMIT)
            )
        if signals.thread_id:
            by_thread = self._db.find_application_by_thread_id(signals.thread_id)
            if by_thread is not None:
                add_apps([by_thread])
        if signals.sender_address:
            pending_ids += [
                app_id
                for app_id, _ in self._db.find_linked_applications_by_sender(
                    signals.sender_address, _SENDER_LIMIT
                )
            ]
        if signals.company_domain:
            pending_ids += self._db.find_linked_applications_by_sender_domain(
                signals.company_domain, _SENDER_LIMIT
            )
        missing = sorted({i for i in pending_ids if i not in ordered})
        add_apps(self._db.get_applications_by_ids(missing))
        if signals.company:
            add_apps(self._db.find_applications_by_company(signals.company, None, _COMPANY_LIMIT))
            first = signals.company.split()[0]
            if len(first) >= 3:
                add_apps(self._db.find_applications_by_company_prefix(first, None, _PREFIX_LIMIT))
        return list(ordered.values())[: app_config.RESOLVER_MAX_CANDIDATES]

    # -- decision -------------------------------------------------------

    def resolve(self, signals: EvidenceSignals) -> ResolutionResult:
        if not signals.has_identity:
            return self._review(signals, (), "insufficient_identity")

        apps = self._candidates(signals)
        identity = self._db.evidence_identities_for_applications([a.id for a in apps if a.id])
        active_same_company = [
            a
            for a in apps
            if signals.company
            and _app_company(a) == signals.company
            and a.current_status not in _TERMINAL
        ]
        sole_id = active_same_company[0].id if len(active_same_company) == 1 else None
        scored = sorted(
            (
                score_candidate(signals, a, identity.get(a.id or -1, {}), a.id == sole_id)
                for a in apps
            ),
            key=lambda c: (-c.score, c.application_id),
        )
        candidates = tuple(scored)
        best = scored[0] if scored else None
        second = scored[1] if len(scored) > 1 else None
        auto = app_config.RESOLVER_AUTO_LINK_SCORE
        margin = app_config.RESOLVER_AUTO_LINK_MARGIN
        review_floor = app_config.RESOLVER_REVIEW_SCORE

        strong_ids = {c.application_id for c in scored if c.strong}
        if len(strong_ids) > 1:
            return self._review(signals, candidates, "conflicting_strong_identifiers")
        if best and best.strong and best.has("external_job_id_conflict"):
            return self._review(signals, candidates, "strong_identifier_conflict")
        if best and strong_ids and best.application_id not in strong_ids:
            # Text similarity favours one application while a strong identifier names
            # another: never let fuzzy evidence override an exact identifier silently.
            return self._review(signals, candidates, "strong_identifier_disagrees_with_text")

        if best and best.score >= auto and (second is None or best.score - second.score >= margin):
            method = self._link_method(best)
            confidence = round(min(1.0, best.score / 120), 3)
            explanation = (
                f"Linked to application #{best.application_id} (score {best.score}"
                + (f", next best {second.score}" if second else "")
                + "): "
                + ", ".join(s.name.replace("_", " ") for s in best.signals if s.weight > 0)
            )
            return ResolutionResult(
                Outcome.LINKED,
                "auto_link",
                confidence,
                best.application_id,
                candidates,
                signals.kind,
                link_method=method,
                explanation=explanation,
            )

        if best and best.score >= review_floor:
            reason = (
                "ambiguous_candidates"
                if second and best.score - second.score < margin and second.score >= review_floor
                else "below_auto_link_threshold"
            )
            return self._review(signals, candidates, reason)
        if strong_ids:
            return self._review(signals, candidates, "strong_identifier_below_threshold")

        if self._credible_new(signals):
            if any(
                c.has("company_exact") and (c.has("role_exact") or c.has("role_similar"))
                for c in scored
            ):
                return self._review(signals, candidates, "possible_reapplication")
            confidence = 0.9 if signals.role else 0.75
            return ResolutionResult(
                Outcome.NEW_APPLICATION,
                "credible_acknowledgement",
                confidence,
                None,
                candidates,
                signals.kind,
                link_method=LinkMethod.CREATED.value,
                explanation=_REASON_TEXT["credible_acknowledgement"],
            )
        if signals.kind is MessageKind.ACKNOWLEDGEMENT:
            return self._review(signals, candidates, "not_a_credible_new_application")
        return self._review(signals, candidates, f"{signals.kind.value}_without_application")

    @staticmethod
    def _credible_new(signals: EvidenceSignals) -> bool:
        return (
            signals.kind is MessageKind.ACKNOWLEDGEMENT
            and not signals.forwarded
            and bool(signals.company)
            and not signals.sender_is_freemail
            and (signals.classification_confident or signals.company_domain is not None)
        )

    @staticmethod
    def _link_method(best: CandidateScore) -> str:
        for signal, method in (
            ("same_gmail_thread", LinkMethod.THREAD),
            ("same_external_job_id", LinkMethod.EXTERNAL_JOB_ID),
            ("same_canonical_job_url", LinkMethod.JOB_URL),
        ):
            if best.has(signal):
                return method.value
        if best.has("role_exact") or best.has("role_similar"):
            return LinkMethod.COMPANY_ROLE.value
        return LinkMethod.COMPANY_ONLY.value

    @staticmethod
    def _review(
        signals: EvidenceSignals, candidates: tuple[CandidateScore, ...], reason: str
    ) -> ResolutionResult:
        best = candidates[0].score if candidates else 0
        top = candidates[0].application_id if candidates else None
        text = _REASON_TEXT.get(reason, reason.replace("_", " "))
        if top is not None:
            text += f" (best candidate #{top}, score {best})"
        return ResolutionResult(
            Outcome.REVIEW_REQUIRED,
            reason,
            round(max(0, min(1.0, best / 120)), 3),
            None,
            candidates,
            signals.kind,
            explanation=text,
        )
