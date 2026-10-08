"""Gmail ingestion through the evidence model — Gmail API fully mocked, synthetic messages.

Covers: one evidence row per message across repeated polls, thread linking, follow-up and
status mail that must not create applications, ambiguity, privacy of non-job mail, and
error handling.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.db.data_store import ApplicationFilter, DataStore, EvidenceFilter
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.parser.email_parser import EmailParser
from backend.poller.gmail_poller import GmailPoller


def days_ago(days: int) -> str:
    """RFC 2822 date inside the 180-day identity lookback window."""
    return format_datetime((utc_now() - timedelta(days=days)).replace(microsecond=0))


def gmail_message(
    msg_id: str,
    thread_id: str,
    sender: str,
    subject: str,
    date: str | None = None,
    snippet: str = "",
) -> dict:
    date = date or days_ago(10)
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": snippet,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date},
            ]
        },
    }


ACK = gmail_message(
    "ack-1",
    "thread-infosys",
    "Naukri <noreply@naukri.com>",
    "Your application to Infosys for Software Engineer",
    snippet="Your application to Infosys has been received",
)
FOLLOW_UP_SAME_THREAD = gmail_message(
    "fu-1",
    "thread-infosys",
    "Naukri <noreply@naukri.com>",
    "Re: Your application to Infosys for Software Engineer",
    date=days_ago(9),
    snippet="Thanks for your interest, more soon",
)


class FakeGmail:
    """A mock Gmail service whose message list can change between polls."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages: dict[str, dict] = {}
        self.service = MagicMock(name="gmail")
        users = self.service.users.return_value
        users.messages.return_value.list.return_value.execute.side_effect = self._list
        users.messages.return_value.get.side_effect = self._get
        self.set(messages)

    def set(self, messages: list[dict]) -> None:
        self.messages = {m["id"]: m for m in messages}

    def _list(self) -> dict:
        return {
            "messages": [
                {"id": m["id"], "threadId": m["threadId"]} for m in self.messages.values()
            ],
            "historyId": "1000",
        }

    def _get(self, **kwargs) -> MagicMock:
        request = MagicMock()
        request.execute.return_value = self.messages[kwargs["id"]]
        return request


@pytest.fixture
def db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "ingest.db")


@pytest.fixture
def poller(db: DataStore) -> GmailPoller:
    poller = GmailPoller(db, EmailParser(), StatusUpdater(db, DuplicateDetector(db)))
    poller._fetch_body_text = MagicMock(return_value="")  # type: ignore[method-assign]
    return poller


def poll(poller: GmailPoller, gmail: FakeGmail) -> int:
    poller.service = gmail.service
    poller.last_history_id = None  # always list (backfill path), like a fresh poll
    return poller.poll_once()


def apps(db: DataStore) -> list[Application]:
    return db.get_applications(ApplicationFilter())[0]


def evidence(db: DataStore, **filters):
    return db.list_evidence(EvidenceFilter(include_ignored=True, **filters))[0]


# ------------------------------------------------------------------ #
# Idempotency                                                          #
# ------------------------------------------------------------------ #


def test_same_message_polled_twice_yields_one_evidence_and_one_application(poller, db) -> None:
    gmail = FakeGmail([ACK])
    assert poll(poller, gmail) == 1
    assert poll(poller, gmail) == 0
    assert len(apps(db)) == 1
    rows = evidence(db)
    assert len(rows) == 1
    assert rows[0].processing_status == "created_application"
    assert rows[0].link_method == "created"
    assert rows[0].application_id == apps(db)[0].id


def test_reprocessing_a_message_reuses_its_evidence(poller, db) -> None:
    """backfill_portal clears processed markers to re-run messages; evidence must not
    duplicate and the application must not be created again."""
    gmail = FakeGmail([ACK])
    poll(poller, gmail)
    first = evidence(db)[0]
    db.clear_processed("ack-1")
    poll(poller, gmail)
    rows = evidence(db)
    assert [r.id for r in rows] == [first.id]
    assert len(apps(db)) == 1
    assert rows[0].application_id == first.application_id
    assert rows[0].link_method == "thread"  # re-run resolves to its own application


# ------------------------------------------------------------------ #
# Linking and the duplicate guard                                      #
# ------------------------------------------------------------------ #


def test_thread_emails_link_to_one_application(poller, db) -> None:
    gmail = FakeGmail([ACK])
    poll(poller, gmail)
    gmail.set([ACK, FOLLOW_UP_SAME_THREAD])
    poll(poller, gmail)

    assert len(apps(db)) == 1
    app = apps(db)[0]
    linked = db.get_evidence_for_application(app.id)
    assert [e.external_id for e in linked] == ["ack-1", "fu-1"]
    assert linked[1].link_method == "thread"
    assert linked[1].processing_status == "linked"
    assert app.last_evidence_at == linked[1].occurred_at


def test_follow_up_on_new_thread_for_known_application_links_instead_of_creating(
    poller, db
) -> None:
    gmail = FakeGmail([ACK])
    poll(poller, gmail)
    reminder = gmail_message(
        "fu-2",
        "thread-other",
        "Naukri <noreply@naukri.com>",
        "Reminder: your application to Infosys for Software Engineer",
    )
    gmail.set([ACK, reminder])
    poll(poller, gmail)
    assert len(apps(db)) == 1
    row = db.get_evidence_by_external_id("gmail", "fu-2")
    assert row.application_id == apps(db)[0].id
    assert row.link_method == "company_role"


def test_scheduling_email_for_unknown_application_does_not_create_one(poller, db) -> None:
    gmail = FakeGmail(
        [
            gmail_message(
                "sched-1",
                "thread-stark",
                "Stark Recruiting <no-reply@greenhouse.io>",
                "Interview scheduled: Stark Industries",
            )
        ]
    )
    assert poll(poller, gmail) == 0
    assert apps(db) == []
    row = db.get_evidence_by_external_id("gmail", "sched-1")
    assert row.processing_status == "needs_review"
    assert row.review_reason == "status_update_without_application"
    assert row.subject == "Interview scheduled: Stark Industries"
    assert db.is_processed("sched-1")


def test_follow_up_without_signal_for_unknown_application_needs_review(poller, db) -> None:
    gmail = FakeGmail(
        [
            gmail_message(
                "fu-3",
                "thread-wayne",
                "Wayne Talent <no-reply@greenhouse.io>",
                "Interview with Wayne Enterprises",
            )
        ]
    )
    poll(poller, gmail)
    assert apps(db) == []
    row = db.get_evidence_by_external_id("gmail", "fu-3")
    assert (row.processing_status, row.review_reason) == (
        "needs_review",
        "follow_up_without_application",
    )
    assert db.count_evidence()["needs_review"] == 1


def test_rejection_for_known_company_links_and_advances_status(poller, db) -> None:
    gmail = FakeGmail([ACK])
    poll(poller, gmail)
    rejection = gmail_message(
        "rej-1",
        "thread-rejection",
        "Naukri <noreply@naukri.com>",
        "Your application to Infosys",
        snippet="We regret to inform you that we will not proceed",
    )
    gmail.set([ACK, rejection])
    poll(poller, gmail)
    assert len(apps(db)) == 1
    app = apps(db)[0]
    assert app.current_status is ApplicationStatus.REJECTED
    row = db.get_evidence_by_external_id("gmail", "rej-1")
    assert row.application_id == app.id
    assert row.link_method == "company_only"
    assert row.link_confidence == pytest.approx(0.6)


def test_ambiguous_status_mail_stays_reviewable_and_changes_nothing(poller, db) -> None:
    for role in ("Data Engineer", "Platform Engineer"):
        db.upsert_application(
            Application(
                company="Infosys",
                role=role,
                source_portal="Naukri",
                applied_date=utc_now(),
                current_status=ApplicationStatus.APPLIED,
            )
        )
    rejection = gmail_message(
        "rej-2",
        "thread-x",
        "Naukri <noreply@naukri.com>",
        "Your application to Infosys",
        snippet="We regret to inform you",
    )
    poll(poller, FakeGmail([rejection]))
    assert len(apps(db)) == 2
    assert {a.current_status for a in apps(db)} == {ApplicationStatus.APPLIED}
    row = db.get_evidence_by_external_id("gmail", "rej-2")
    assert (row.processing_status, row.review_reason, row.application_id) == (
        "needs_review",
        "ambiguous_company",
        None,
    )


# ------------------------------------------------------------------ #
# Privacy and other classifications                                    #
# ------------------------------------------------------------------ #


def test_non_job_mail_is_recorded_minimally(poller, db) -> None:
    personal = gmail_message(
        "p-1", "thread-p", "A Friend <friend@example.com>", "Dinner on Friday?", snippet="See you"
    )
    poll(poller, FakeGmail([personal]))
    row = db.get_evidence_by_external_id("gmail", "p-1")
    assert row.processing_status == "ignored"
    assert (row.sender, row.subject, row.snippet, row.normalized_subject) == (None,) * 4
    assert row.raw_metadata == {}
    assert db.list_evidence(EvidenceFilter())[1] == 0  # hidden from default listing


def test_linkedin_prospect_becomes_informational_evidence(poller, db) -> None:
    outreach = gmail_message(
        "li-1",
        "thread-li",
        "LinkedIn <messages-noreply@linkedin.com>",
        "New message from Jane Recruiter",
        snippet="Career opportunity at Wayne Enterprises",
    )
    poll(poller, FakeGmail([outreach]))
    row = db.get_evidence_by_external_id("gmail", "li-1")
    assert row.processing_status == "informational"
    assert row.raw_metadata["classification"] == "prospect"
    assert row.subject == "New message from Jane Recruiter"
    assert len(db.get_prospects()) == 1


def test_message_bodies_are_never_stored(poller, db, tmp_path) -> None:
    """When company/role are missing the poller reads the body to refine them; the body
    must stay in memory only."""
    poller._fetch_body_text = MagicMock(  # type: ignore[method-assign]
        return_value="UNIQUE-BODY-MARKER Confidential salary details"
    )
    vague = gmail_message(
        "vague-1", "thread-v", "Naukri <noreply@naukri.com>", "Application received"
    )
    poll(poller, FakeGmail([vague]))
    poller._fetch_body_text.assert_called()
    conn = sqlite3.connect(tmp_path / "ingest.db")
    try:
        for table in ("evidence", "application", "prospect", "statushistory"):
            for row in conn.execute(f"SELECT * FROM {table}"):
                assert "UNIQUE-BODY-MARKER" not in repr(row), table
    finally:
        conn.close()


def test_processing_error_marks_evidence_and_retries_next_poll(poller, db) -> None:
    gmail = FakeGmail([ACK])
    with patch.object(poller._updater, "process_evidence", side_effect=RuntimeError("boom")):
        poll(poller, gmail)
    row = db.get_evidence_by_external_id("gmail", "ack-1")
    assert (row.processing_status, row.review_reason) == ("error", "RuntimeError")
    assert not db.is_processed("ack-1")  # retried on the next poll
    poll(poller, gmail)
    assert db.get_evidence_by_external_id("gmail", "ack-1").processing_status == (
        "created_application"
    )
    assert len(evidence(db)) == 1


def test_parser_metadata_is_recorded_without_body(poller, db) -> None:
    poll(poller, FakeGmail([ACK]))
    meta = db.get_evidence_by_external_id("gmail", "ack-1").raw_metadata
    assert meta["classification"] == "acknowledgement"
    assert meta["parser"]["portal"] == "Naukri"
    assert meta["parser"]["status_signal"] is None
    assert set(meta) == {"classification", "parser"}
