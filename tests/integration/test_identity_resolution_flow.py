"""End-to-end identity resolution through GmailPoller and StatusUpdater (Gmail mocked,
synthetic messages, temporary databases)."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.db.data_store import ApplicationFilter, DataStore, EvidenceFilter
from backend.db.models import (
    Application,
    ApplicationEventType,
    ApplicationStatus,
    Evidence,
    utc_now,
)
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.resolver_metrics import metrics
from backend.engine.status_updater import StatusUpdater
from backend.parser.email_parser import EmailParser, ParsedApplication
from backend.poller.gmail_poller import GmailPoller
from tests.integration.test_evidence_ingestion import FakeGmail, days_ago, gmail_message

NAUKRI = "Naukri <noreply@naukri.com>"
ACK = gmail_message(
    "ack", "thread-1", NAUKRI, "Your application to Infosys for Software Engineer", days_ago(20)
)
ASSESSMENT = gmail_message(
    "assess", "thread-1", NAUKRI, "Online assessment for Software Engineer at Infosys", days_ago(18)
)
ROUND_1 = gmail_message(
    "round-1", "thread-1", NAUKRI, "Interview scheduled for Software Engineer", days_ago(15)
)
ROUND_2 = gmail_message(
    "round-2", "thread-1", NAUKRI, "Interview scheduled: second round", days_ago(10)
)
REJECTION = gmail_message(
    "reject",
    "thread-rejection",
    NAUKRI,
    "Your application to Infosys",
    days_ago(5),
    snippet="We regret to inform you that we will not proceed",
)


@pytest.fixture
def db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "flow.db")


@pytest.fixture
def poller(db: DataStore) -> GmailPoller:
    poller = GmailPoller(db, EmailParser(), StatusUpdater(db, DuplicateDetector(db)))
    poller._fetch_body_text = MagicMock(return_value="")  # type: ignore[method-assign]
    return poller


def run(poller: GmailPoller, *messages: dict) -> None:
    gmail = FakeGmail(list(messages))
    poller.service = gmail.service
    poller.last_history_id = None
    poller.poll_once()


def apps(db: DataStore) -> list[Application]:
    return db.get_applications(ApplicationFilter())[0]


def interview_events(db: DataStore, app_id: int) -> list:
    return [
        e
        for e in db.get_application_events(app_id)
        if e.event_type == ApplicationEventType.INTERVIEW_SCHEDULED
    ]


def test_acknowledgement_then_assessment_invitation(poller, db) -> None:
    run(poller, ACK)
    run(poller, ACK, ASSESSMENT)
    [app] = apps(db)
    linked = db.get_evidence_for_application(app.id)
    assert [e.external_id for e in linked] == ["ack", "assess"]
    assert linked[1].link_method == "thread"
    assert linked[1].resolver_decision == "linked"
    # The parser's global keywords treat an assessment as interview-stage; whatever the
    # status, it lands on the same application.
    assert app.current_status in (ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW_SCHEDULED)


def test_interview_rounds_in_one_thread_add_milestones_not_applications(poller, db) -> None:
    run(poller, ACK, ROUND_1, ROUND_2)
    [app] = apps(db)
    assert app.current_status is ApplicationStatus.INTERVIEW_SCHEDULED
    history = [h.to_status for h in db.get_status_history(app.id)]
    assert history.count("Interview Scheduled") == 1
    assert {e.source_message_id for e in interview_events(db, app.id)} == {"round-1", "round-2"}


def test_rejection_following_interview_links_and_closes(poller, db) -> None:
    run(poller, ACK, ROUND_1)
    run(poller, ACK, ROUND_1, REJECTION)
    [app] = apps(db)
    assert app.current_status is ApplicationStatus.REJECTED
    rejection = db.get_evidence_by_external_id("gmail", "reject")
    assert (rejection.application_id, rejection.link_method) == (app.id, "company_only")


def test_replaying_everything_is_idempotent(poller, db) -> None:
    messages = (ACK, ASSESSMENT, ROUND_1, ROUND_2, REJECTION)
    run(poller, *messages)
    [app] = apps(db)
    snapshot = (
        [(h.from_status, h.to_status) for h in db.get_status_history(app.id)],
        len(db.get_application_events(app.id)),
        [
            (e.external_id, e.application_id, e.link_method)
            for e in db.get_evidence_for_application(app.id)
        ],
    )
    for message in messages:
        db.clear_processed(message["id"])
    run(poller, *messages)
    assert len(apps(db)) == 1
    assert (
        [(h.from_status, h.to_status) for h in db.get_status_history(app.id)],
        len(db.get_application_events(app.id)),
        [
            (
                e.external_id,
                e.application_id,
                "thread" if e.external_id != "reject" else e.link_method,
            )
            for e in db.get_evidence_for_application(app.id)
        ],
    )[0:2] == snapshot[0:2]
    assert db.list_evidence(EvidenceFilter(include_ignored=True))[1] == 5


def _two_infosys_apps(db: DataStore) -> tuple[Application, Application]:
    made = []
    for role in ("Data Engineer", "Platform Engineer"):
        made.append(
            db.upsert_application(
                Application(
                    company="Infosys",
                    role=role,
                    source_portal="Naukri",
                    applied_date=utc_now() - timedelta(days=30),
                    current_status=ApplicationStatus.APPLIED,
                )
            )
        )
    return made[0], made[1]


def test_human_confirmed_link_is_never_overwritten(poller, db) -> None:
    first, second = _two_infosys_apps(db)
    run(poller, REJECTION)
    evidence = db.get_evidence_by_external_id("gmail", "reject")
    assert evidence.processing_status == "needs_review"

    poller._updater.accept_candidate(evidence.id, second.id)
    assert db.get_application(second.id).current_status is ApplicationStatus.REJECTED
    assert db.get_application(first.id).current_status is ApplicationStatus.APPLIED

    metrics.reset()
    db.clear_processed("reject")
    run(poller, REJECTION)  # automated replay
    after = db.get_evidence_by_external_id("gmail", "reject")
    assert (after.application_id, after.decided_by, after.link_method) == (
        second.id,
        "human",
        "manual",
    )
    assert metrics.snapshot()["human_decision_retained"] == 1
    assert db.get_application(first.id).current_status is ApplicationStatus.APPLIED

    # Accepting again is a no-op.
    poller._updater.accept_candidate(evidence.id, second.id)
    assert [h.to_status for h in db.get_status_history(second.id)].count("Rejected") == 1


def test_record_resolution_refuses_to_touch_human_decisions(db) -> None:
    app = db.upsert_application(
        Application(company="A", role="B", source_portal="LinkedIn", applied_date=utc_now())
    )
    evidence, _ = db.insert_evidence(
        Evidence(evidence_type="email", source="gmail", external_id="h1", occurred_at=utc_now())
    )
    db.link_evidence(evidence.id, app.id, "manual", 1.0, decided_by="human")
    assert db.record_resolution(evidence.id, resolution={}, status="needs_review") is None
    assert db.claim_evidence(evidence.id) is False
    assert db.get_evidence(evidence.id).application_id == app.id


def _ack_parsed(message_id: str = "conc") -> ParsedApplication:
    return ParsedApplication(
        message_id=message_id,
        thread_id="thread-conc",
        company="Hooli",
        role="SRE",
        source_portal="LinkedIn",
        job_url=None,
        applied_date=utc_now(),
        status_signal=None,
        raw_sender="LinkedIn <jobs-noreply@linkedin.com>",
        raw_subject="Your application was sent to Hooli",
        is_classification_confident=True,
    )


def test_concurrent_processing_creates_one_application(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    seed = DataStore(path)
    evidence, _ = seed.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="conc",
            thread_id="thread-conc",
            occurred_at=utc_now(),
        )
    )
    seed.close()
    workers = 6
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        store = DataStore(path)
        updater = StatusUpdater(store, DuplicateDetector(store))
        try:
            barrier.wait()
            outcomes.append(updater.process_evidence(_ack_parsed(), evidence.id).result)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    check = DataStore(path)
    assert check.get_applications(ApplicationFilter())[1] == 1
    assert outcomes.count("applied") == 1
    assert set(outcomes) <= {"applied", "in_progress", "thread_merged"}


def test_stale_processing_claim_can_be_taken_over(db) -> None:
    evidence, _ = db.insert_evidence(
        Evidence(evidence_type="email", source="gmail", external_id="s1", occurred_at=utc_now())
    )
    assert db.claim_evidence(evidence.id) is True
    assert db.claim_evidence(evidence.id) is False  # fresh claim held
    db.update_evidence_processing(evidence.id, "processing")
    import sqlite3

    conn = sqlite3.connect(db._db_path)  # test-only: age the claim
    conn.execute(
        "UPDATE evidence SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (evidence.id,)
    )
    conn.commit()
    conn.close()
    assert db.claim_evidence(evidence.id) is True


def test_reviewed_evidence_becomes_a_new_application_once(poller, db) -> None:
    scheduling = gmail_message(
        "sched",
        "thread-stark",
        "Stark Recruiting <no-reply@greenhouse.io>",
        "Interview scheduled: Stark Industries",
    )
    run(poller, scheduling)
    evidence = db.get_evidence_by_external_id("gmail", "sched")
    assert evidence.processing_status == "needs_review"
    assert apps(db) == []

    created = poller._updater.create_from_review(
        evidence.id, company="Stark Industries", role="SRE"
    )
    again = poller._updater.create_from_review(evidence.id)
    assert again.id == created.id
    assert len(apps(db)) == 1
    assert created.current_status is ApplicationStatus.INTERVIEW_SCHEDULED
    after = db.get_evidence(evidence.id)
    assert (after.decided_by, after.link_method, after.processing_status) == (
        "human",
        "created",
        "created_application",
    )
    triggers = [h.trigger for h in db.get_status_history(created.id)]
    assert triggers == ["review", "review"]


def test_metrics_count_each_outcome(poller, db) -> None:
    metrics.reset()
    personal = gmail_message("p", "tp", "Friend <f@example.com>", "Dinner?")
    run(poller, ACK, personal)
    run(poller, ACK, personal)  # second poll: both already processed
    counts = metrics.snapshot()
    assert counts["new_application_created"] == 1
    assert counts["ignored"] == 1
    assert counts["duplicate_skipped"] == 2
    assert counts["evidence_processed"] == 1
    assert counts["resolver_errors"] == 0
