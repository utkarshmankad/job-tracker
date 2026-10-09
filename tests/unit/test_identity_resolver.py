"""Table-driven tests for backend/engine/identity_resolver.py (resolver v2)."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend import config
from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, Evidence, utc_now
from backend.engine.identity_resolver import (
    RESOLVER_VERSION,
    EvidenceSignals,
    IdentityResolver,
    MessageKind,
    Outcome,
    classify_message_kind,
    compare_applications,
    signals_from_parsed,
)
from backend.parser.email_parser import ParsedApplication

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = utc_now().replace(microsecond=0)
ACK, FOLLOW, STATUS = MessageKind.ACKNOWLEDGEMENT, MessageKind.FOLLOW_UP, MessageKind.STATUS_UPDATE


@dataclass
class AppSpec:
    company: str | None
    role: str | None
    days_ago: int = 10
    status: ApplicationStatus = ApplicationStatus.APPLIED
    threads: tuple[str, ...] = ()
    job_url: str | None = None
    external_job_id: str | None = None
    source_portal: str = "LinkedIn"


@dataclass
class PriorLink:
    """Earlier evidence from a sender, already linked to an application."""

    app: str
    sender: str
    decided_by: str = "resolver"


@dataclass
class Case:
    name: str
    apps: dict[str, AppSpec]
    signals: dict[str, Any]
    outcome: Outcome
    selected: str | None = None
    reason: str | None = None
    link_method: str | None = None
    priors: list[PriorLink] = field(default_factory=list)


def _sig(**overrides: Any) -> EvidenceSignals:
    base: dict[str, Any] = {"occurred_at": NOW, "kind": FOLLOW}
    base.update(overrides)
    return EvidenceSignals(**base)


CASES = [
    # --- strong identifiers ------------------------------------------------
    Case(
        "known Gmail thread links",
        {"a": AppSpec("Acme", "Engineer", threads=("t-1",))},
        {"thread_id": "t-1"},
        Outcome.LINKED,
        "a",
        "auto_link",
        "thread",
    ),
    Case(
        "changed subject / noisy extraction in the same thread still links",
        {"a": AppSpec("Acme", "Backend Engineer", threads=("t-1",))},
        {"thread_id": "t-1", "company": "talent acquisition team", "role": "next steps"},
        Outcome.LINKED,
        "a",
        "auto_link",
        "thread",
    ),
    Case(
        "exact external job ID within the same source links",
        {
            "a": AppSpec(
                "Acme", "Engineer", job_url="https://www.linkedin.com/jobs/view/3912345678/"
            )
        },
        {"external_job_id": "3912345678", "external_job_source": "linkedin", "kind": STATUS},
        Outcome.LINKED,
        "a",
        "auto_link",
        "external_job_id",
    ),
    Case(
        "same job ID on a different source is only a weak hint",
        {"a": AppSpec("Acme", "Engineer", external_job_id="555666777", source_portal="Naukri")},
        {"external_job_id": "555666777", "external_job_source": "linkedin", "kind": STATUS},
        Outcome.REVIEW_REQUIRED,
        None,
        "status_update_without_application",
    ),
    Case(
        "exact canonical job URL links",
        {"a": AppSpec("Acme", "Engineer", job_url="https://careers.acme.com/jobs/77?utm_source=x")},
        {"canonical_url": "https://careers.acme.com/jobs/77", "kind": STATUS},
        Outcome.LINKED,
        "a",
        "auto_link",
        "job_url",
    ),
    Case(
        "conflicting strong identifiers force review",
        {
            "a": AppSpec("Acme", "Engineer", threads=("t-1",)),
            "b": AppSpec("Globex", "Analyst", job_url="https://jobs.globex.com/9"),
        },
        {"thread_id": "t-1", "canonical_url": "https://jobs.globex.com/9"},
        Outcome.REVIEW_REQUIRED,
        None,
        "conflicting_strong_identifiers",
    ),
    Case(
        "thread match contradicted by a different job ID forces review",
        {
            "a": AppSpec(
                "Acme",
                "Engineer",
                threads=("t-1",),
                job_url="https://www.linkedin.com/jobs/view/1111111",
            )
        },
        {"thread_id": "t-1", "external_job_id": "2222222", "external_job_source": "linkedin"},
        Outcome.REVIEW_REQUIRED,
        None,
        "strong_identifier_conflict",
    ),
    # --- company / role ----------------------------------------------------
    Case(
        "duplicate acknowledgement for a recent identical application links",
        {"a": AppSpec("Acme Pvt Ltd", "Software Engineer", days_ago=3)},
        {"kind": ACK, "company": "acme", "role": "software engineer", "source": "linkedin"},
        Outcome.LINKED,
        "a",
        "auto_link",
        "company_role",
    ),
    Case(
        "same company, different role does not link",
        {"a": AppSpec("Acme", "Data Engineer")},
        {"kind": ACK, "company": "acme", "role": "sales manager"},
        Outcome.NEW_APPLICATION,
        None,
        "credible_acknowledgement",
    ),
    Case(
        "same role at a different company does not link",
        {"a": AppSpec("Acme", "Software Engineer")},
        {"kind": ACK, "company": "globex", "role": "software engineer"},
        Outcome.NEW_APPLICATION,
        None,
        "credible_acknowledgement",
    ),
    Case(
        "same company and role months apart goes to review",
        {"a": AppSpec("Acme", "Engineer", days_ago=300, status=ApplicationStatus.REJECTED)},
        {"kind": ACK, "company": "acme", "role": "engineer"},
        Outcome.REVIEW_REQUIRED,
        None,
        "possible_reapplication",
    ),
    Case(
        "near-variant company name is plausible but not auto-linked",
        {"a": AppSpec("Acme", "Software Engineer")},
        {"kind": ACK, "company": "acme india", "role": "software engineer"},
        Outcome.REVIEW_REQUIRED,
        None,
        "below_auto_link_threshold",
    ),
    Case(
        "same company, two roles, status mail without role is ambiguous",
        {"a": AppSpec("Acme", "Data Engineer"), "b": AppSpec("Acme", "Platform Engineer")},
        {"kind": STATUS, "company": "acme", "status_signal": ApplicationStatus.REJECTED},
        Outcome.REVIEW_REQUIRED,
        None,
        "ambiguous_candidates",
    ),
    Case(
        "rejection after interview on a new thread links to the sole active application",
        {"a": AppSpec("Acme", "Engineer", status=ApplicationStatus.INTERVIEW_SCHEDULED)},
        {
            "kind": STATUS,
            "company": "acme",
            "status_signal": ApplicationStatus.REJECTED,
            "source": "linkedin",
        },
        Outcome.LINKED,
        "a",
        "auto_link",
        "company_only",
    ),
    # --- senders, vendors, recruiters --------------------------------------
    Case(
        "recruiter domain alone is not enough",
        {"a": AppSpec("Acme", "Engineer")},
        {"sender_address": "jane@talentpartners.io", "company_domain": "talentpartners.io"},
        Outcome.REVIEW_REQUIRED,
        None,
        "below_auto_link_threshold",
        priors=[PriorLink("a", "jane@talentpartners.io")],
    ),
    Case(
        "recruiter domain links with supporting company evidence",
        {"a": AppSpec("Acme", "Engineer")},
        {
            "sender_address": "jane@talentpartners.io",
            "company_domain": "talentpartners.io",
            "company": "acme",
        },
        Outcome.LINKED,
        "a",
        "auto_link",
        priors=[PriorLink("a", "jane@talentpartners.io")],
    ),
    Case(
        "recruiter previously confirmed by a person links scheduling mail",
        {"a": AppSpec("Stark Industries", "Engineer")},
        {
            "sender_address": "pepper@stark.com",
            "company_domain": "stark.com",
            "company": "stark industries",
        },
        Outcome.LINKED,
        "a",
        "auto_link",
        priors=[PriorLink("a", "pepper@stark.com", decided_by="human")],
    ),
    Case(
        "recruiter outreach followed by scheduling with no application is reviewed",
        {},
        {"sender_address": "pepper@stark.com", "company": "stark industries"},
        Outcome.REVIEW_REQUIRED,
        None,
        "follow_up_without_application",
    ),
    # --- new-application credibility --------------------------------------
    Case(
        "credible acknowledgement with no candidate creates",
        {},
        {"kind": ACK, "company": "initech", "role": "sre"},
        Outcome.NEW_APPLICATION,
        None,
        "credible_acknowledgement",
    ),
    Case(
        "forwarded acknowledgement never creates",
        {},
        {"kind": ACK, "company": "initech", "role": "sre", "forwarded": True},
        Outcome.REVIEW_REQUIRED,
        None,
        "not_a_credible_new_application",
    ),
    Case(
        "free-mail acknowledgement never creates",
        {},
        {"kind": ACK, "company": "initech", "role": "sre", "sender_is_freemail": True},
        Outcome.REVIEW_REQUIRED,
        None,
        "not_a_credible_new_application",
    ),
    Case(
        "unconfident classification needs an employer domain to create",
        {},
        {"kind": ACK, "company": "initech", "classification_confident": False},
        Outcome.REVIEW_REQUIRED,
        None,
        "not_a_credible_new_application",
    ),
    Case(
        "unconfident classification from an employer domain creates",
        {},
        {
            "kind": ACK,
            "company": "initech",
            "classification_confident": False,
            "company_domain": "initech.com",
        },
        Outcome.NEW_APPLICATION,
        None,
        "credible_acknowledgement",
    ),
    Case(
        "portal status message without any confirmation email is reviewed",
        {},
        {"kind": STATUS, "company": "acme", "source": "naukri"},
        Outcome.REVIEW_REQUIRED,
        None,
        "status_update_without_application",
    ),
    Case(
        "missing company and role with only an unknown thread is reviewed",
        {"a": AppSpec("Acme", "Engineer")},
        {"thread_id": "t-unknown"},
        Outcome.REVIEW_REQUIRED,
        None,
        "follow_up_without_application",
    ),
    Case(
        "nothing identifying at all",
        {"a": AppSpec("Acme", "Engineer")},
        {},
        Outcome.REVIEW_REQUIRED,
        None,
        "insufficient_identity",
    ),
]


def _seed(db: DataStore, case: Case) -> dict[str, int]:
    ids: dict[str, int] = {}
    for key, spec in case.apps.items():
        app = db.upsert_application(
            Application(
                company=spec.company,
                role=spec.role,
                source_portal=spec.source_portal,
                job_url=spec.job_url,
                external_job_id=spec.external_job_id,
                applied_date=NOW - timedelta(days=spec.days_ago),
                current_status=spec.status,
                thread_ids=json.dumps(list(spec.threads)),
            )
        )
        assert app.id is not None
        ids[key] = app.id
    for n, prior in enumerate(case.priors):
        evidence, _ = db.insert_evidence(
            Evidence(
                evidence_type="email",
                source="gmail",
                external_id=f"prior-{n}",
                sender=f"Recruiter <{prior.sender}>",
                occurred_at=NOW - timedelta(days=5),
            )
        )
        db.link_evidence(evidence.id, ids[prior.app], "manual", 1.0, decided_by=prior.decided_by)
    return ids


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_identity_matrix(tmp_path: Path, case: Case) -> None:
    db = DataStore(tmp_path / "matrix.db")
    ids = _seed(db, case)
    result = IdentityResolver(db).resolve(_sig(**case.signals))
    assert result.outcome is case.outcome, result.explanation
    assert result.application_id == (ids[case.selected] if case.selected else None)
    if case.reason:
        assert result.reason == case.reason, result.explanation
    if case.link_method:
        assert result.link_method == case.link_method
    assert result.version == RESOLVER_VERSION
    assert result.explanation
    if result.outcome is Outcome.LINKED:
        assert result.confidence >= config.RESOLVER_AUTO_LINK_SCORE / 120
    else:
        assert result.outcome is not Outcome.LINKED  # low confidence never auto-links


# ------------------------------------------------------------------ #
# Scoring properties                                                   #
# ------------------------------------------------------------------ #


def test_strong_identifier_outweighs_fuzzy_similarity(tmp_path: Path) -> None:
    db = DataStore(tmp_path / "strong.db")
    url_app = db.upsert_application(
        Application(
            company="Globex",
            role="Analyst",
            source_portal="LinkedIn",
            job_url="https://jobs.example.com/1",
            applied_date=NOW,
        )
    )
    db.upsert_application(
        Application(company="Acme", role="Engineer", source_portal="LinkedIn", applied_date=NOW)
    )
    other = db.get_applications_by_ids([url_app.id + 1])[0]
    # Text alone points at the other application; the exact URL names this one. Fuzzy
    # similarity must not silently win over an exact identifier: a person decides.
    result = IdentityResolver(db).resolve(
        _sig(company="acme", role="engineer", canonical_url="https://jobs.example.com/1")
    )
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert result.reason == "strong_identifier_disagrees_with_text"
    assert {c.application_id for c in result.candidates} == {url_app.id, other.id}
    # With no contradicting text the exact URL links on its own.
    linked = IdentityResolver(db).resolve(_sig(canonical_url="https://jobs.example.com/1"))
    assert (linked.outcome, linked.application_id) == (Outcome.LINKED, url_app.id)


def test_candidates_carry_explainable_signals(tmp_path: Path) -> None:
    db = DataStore(tmp_path / "explain.db")
    db.upsert_application(
        Application(company="Acme", role="Engineer", source_portal="LinkedIn", applied_date=NOW)
    )
    result = IdentityResolver(db).resolve(_sig(kind=ACK, company="acme", role="engineer"))
    payload = result.to_json()
    signals = payload["candidates"][0]["signals"]
    assert signals == {
        "company_exact": 35,
        "role_exact": 35,
        "date_in_window": 10,
    }
    assert payload["candidates"][0]["score"] == 80
    assert payload["thresholds"] == {
        "auto_link_score": 80,
        "auto_link_margin": 25,
        "review_score": 40,
    }
    assert payload["outcome"] == "linked" and payload["selected_application_id"] == 1


def test_persisted_result_contains_no_message_content(tmp_path: Path) -> None:
    db = DataStore(tmp_path / "privacy.db")
    result = IdentityResolver(db).resolve(
        _sig(
            kind=ACK,
            company="initech",
            role="sre",
            sender_address="peter.gibbons@initech.com",
            company_domain="initech.com",
        )
    )
    text = json.dumps(result.to_json())
    for forbidden in ("peter.gibbons", "initech.com", "@"):
        assert forbidden not in text


def test_candidate_generation_is_bounded_and_indexed(tmp_path: Path) -> None:
    db = DataStore(tmp_path / "bounded.db")
    for i in range(80):
        db.upsert_application(
            Application(
                company="Acme",
                role=f"Role {i}",
                source_portal="LinkedIn",
                applied_date=NOW - timedelta(days=i),
            )
        )
    with patch.object(db, "get_applications", side_effect=AssertionError("full scan")):
        result = IdentityResolver(db).resolve(_sig(kind=STATUS, company="acme"))
    assert len(result.candidates) <= config.RESOLVER_MAX_CANDIDATES
    assert result.outcome is Outcome.REVIEW_REQUIRED


def test_resolution_is_deterministic_within_a_process(tmp_path: Path) -> None:
    db = DataStore(tmp_path / "det.db")
    for company, role in [("Acme", "Data Engineer"), ("Acme", "Platform Engineer")]:
        db.upsert_application(
            Application(company=company, role=role, source_portal="LinkedIn", applied_date=NOW)
        )
    signals = _sig(kind=STATUS, company="acme")
    outputs = {json.dumps(IdentityResolver(db).resolve(signals).to_json()) for _ in range(5)}
    assert len(outputs) == 1


def test_resolution_is_stable_across_process_restarts(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    db = DataStore(path)
    for company, role, days in [("Acme", "Data Engineer", 3), ("Acme", "Platform Engineer", 9)]:
        db.upsert_application(
            Application(
                company=company,
                role=role,
                source_portal="LinkedIn",
                applied_date=NOW - timedelta(days=days),
            )
        )
    db.close()
    code = (
        "import json, sys;"
        "from tests import isolation; isolation.install();"
        "from datetime import datetime;"
        "from pathlib import Path;"
        "from backend.db.data_store import DataStore;"
        "from backend.engine.identity_resolver import ("
        " IdentityResolver, EvidenceSignals, MessageKind);"
        "db = DataStore(Path(sys.argv[1]));"
        "s = EvidenceSignals(occurred_at=datetime.fromisoformat(sys.argv[2]),"
        " kind=MessageKind.STATUS_UPDATE, company='acme');"
        "print(json.dumps(IdentityResolver(db).resolve(s).to_json(), sort_keys=True))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code, str(path), NOW.isoformat()],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            timeout=120,
            check=True,
        )
        .stdout.strip()
        .splitlines()[-1]
        for seed in ("1", "2", "3")
    }
    assert len(runs) == 1
    assert json.loads(runs.pop())["reason"] == "ambiguous_candidates"


# ------------------------------------------------------------------ #
# Signals and helpers                                                  #
# ------------------------------------------------------------------ #


def _parsed(
    subject: str, sender: str = "Acme Jobs <jobs@acme.com>", **kw: Any
) -> ParsedApplication:
    values: dict[str, Any] = dict(
        message_id="m",
        thread_id="T-ABC",
        company="Acme Pvt Ltd",
        role="Sr. Engineer",
        source_portal="Greenhouse",
        job_url="https://boards.greenhouse.io/acme/jobs/4567890?gh_src=x",
        applied_date=NOW,
        status_signal=None,
        raw_sender=sender,
        raw_subject=subject,
        is_classification_confident=True,
    )
    values.update(kw)
    return ParsedApplication(**values)


def test_signals_from_parsed_normalizes_everything() -> None:
    s = signals_from_parsed(_parsed("Your application to Acme"))
    assert (s.company, s.role, s.thread_id, s.source) == (
        "acme",
        "senior engineer",
        "t-abc",
        "company_portal",
    )
    assert s.external_job_id == "4567890" and s.external_job_source == "company_portal"
    assert s.canonical_url == "https://boards.greenhouse.io/acme/jobs/4567890"
    assert s.sender_address is None  # shared "jobs@" mailbox is not a personal identity
    assert s.company_domain == "acme.com"
    assert s.kind is ACK and not s.forwarded


def test_forwarded_messages_drop_sender_signals() -> None:
    s = signals_from_parsed(_parsed("Fwd: Interview with Acme", sender="Me <me@gmail.com>"))
    assert s.forwarded is True
    assert s.sender_address is None and s.company_domain is None
    assert s.kind is FOLLOW


@pytest.mark.parametrize(
    ("subject", "status", "expected"),
    [
        ("Your application to Acme", None, ACK),
        ("Application received - Engineer", None, ACK),
        ("Re: Your application to Acme", None, FOLLOW),
        ("Interview with Acme", None, FOLLOW),
        ("Online assessment for Engineer", None, FOLLOW),
        ("Please share your availability", None, FOLLOW),
        ("Your application to Acme", ApplicationStatus.REJECTED, STATUS),
    ],
)
def test_classify_message_kind(subject, status, expected) -> None:
    assert classify_message_kind(_parsed(subject, status_signal=status)) is expected


def test_compare_applications_for_duplicate_suggestions() -> None:
    a = Application(
        id=1, company="Acme Pvt Ltd", role="Engineer", source_portal="LinkedIn", applied_date=NOW
    )
    b = replace_app(a, id=2, company="Acme", source_portal="Naukri")
    score, reasons = compare_applications(a, b)
    assert score >= config.DUPLICATE_SUGGESTION_SCORE
    assert "Same normalized company" in reasons
    c = replace_app(a, id=3, role="Sales Manager")
    assert compare_applications(a, c)[0] < config.DUPLICATE_SUGGESTION_SCORE


def replace_app(app: Application, **changes: Any) -> Application:
    data = app.model_dump()
    data.update(changes)
    return Application(**data)


def test_signals_dataclass_is_immutable() -> None:
    s = _sig()
    with pytest.raises(AttributeError):
        s.company = "x"  # type: ignore[misc]
    assert replace(s, company="x").company == "x"
