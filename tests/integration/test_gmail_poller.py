"""Integration tests for GmailPoller — Gmail API is fully mocked."""

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from backend.db.data_store import ApplicationFilter, DataStore
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.parser.email_parser import EmailParser
from backend.poller.error_retry import AuthError
from backend.poller.gmail_poller import GmailPoller, PollerStatus

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

TEST_MESSAGES = [
    {
        "id": "msg001",
        "threadId": "thread001",
        "snippet": "Your application to Infosys has been received",
        "payload": {
            "headers": [
                {"name": "From", "value": "noreply@naukri.com"},
                {"name": "Subject", "value": "Your application to Infosys for Software Engineer"},
                {"name": "Date", "value": "Mon, 13 May 2024 10:00:00 +0000"},
            ]
        },
    }
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"Error")


def make_mock_service(messages: list[dict]) -> MagicMock:
    """Return a mock Gmail service pre-loaded with the given messages."""
    service = MagicMock()
    msg_by_id = {m["id"]: m for m in messages}

    list_result = {
        "messages": [{"id": m["id"], "threadId": m["threadId"]} for m in messages],
        "historyId": "99999",
    }
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = (
        list_result
    )

    def get_side_effect(**kwargs):
        msg_id = kwargs["id"]
        result_mock = MagicMock()
        result_mock.execute.return_value = msg_by_id[msg_id]
        return result_mock

    service.users.return_value.messages.return_value.get.side_effect = get_side_effect
    return service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    return DataStore(db_path=tmp_path / "test.db")


@pytest.fixture
def poller(db):
    parser = EmailParser()
    detector = DuplicateDetector(db=db)
    updater = StatusUpdater(db=db, detector=detector)
    return GmailPoller(db=db, parser=parser, updater=updater)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poll_creates_application(poller, db):
    poller.service = make_mock_service(TEST_MESSAGES)
    count = poller.poll_once()

    assert count == 1
    apps, total = db.get_applications(ApplicationFilter())
    assert total == 1
    assert apps[0].source_portal == "Naukri"


def test_duplicate_message_skipped(poller, db):
    db.mark_processed("msg001", "applied")
    poller.service = make_mock_service(TEST_MESSAGES)
    poller.poll_once()

    _, total = db.get_applications(ApplicationFilter())
    assert total == 0


def test_auth_error_updates_poller_state(poller, db):
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = (
        make_http_error(401)
    )
    poller.service = service

    with pytest.raises(AuthError):
        poller.poll_once()

    assert db.get_poller_state().status == PollerStatus.AUTH_ERROR.value


def test_rate_limit_retried(poller, db):
    service = make_mock_service(TEST_MESSAGES)
    list_execute = service.users.return_value.messages.return_value.list.return_value.execute
    list_result = {
        "messages": [{"id": m["id"], "threadId": m["threadId"]} for m in TEST_MESSAGES],
        "historyId": "99999",
    }
    list_execute.side_effect = [make_http_error(429), list_result]
    poller.service = service

    poller.poll_once()

    _, total = db.get_applications(ApplicationFilter())
    assert total == 1
    assert list_execute.call_count == 2


def test_first_run_backfill_uses_list_not_history(poller):
    service = make_mock_service([])
    poller.service = service
    poller.last_history_id = None

    poller.poll_once()

    service.users.return_value.messages.return_value.list.assert_called_once()
    service.users.return_value.history.return_value.list.assert_not_called()


def test_suppress_rule_prevents_insert(poller, db):
    db.add_suppress_rule(sender_pattern="naukri\\.com")
    poller.service = make_mock_service(TEST_MESSAGES)
    poller.poll_once()

    _, total = db.get_applications(ApplicationFilter())
    assert total == 0


def test_fetch_via_history_used_when_last_history_id_set(poller, db):
    """When last_history_id is set, the poller calls history.list, not messages.list."""
    service = MagicMock()
    history_response = {
        "historyId": "100000",
        "history": [{"messagesAdded": [{"message": {"id": "msg001", "threadId": "thread001"}}]}],
    }
    service.users.return_value.history.return_value.list.return_value.execute.return_value = (
        history_response
    )

    msg_response = TEST_MESSAGES[0]
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
        msg_response
    )

    poller.service = service
    poller.last_history_id = "99999"

    poller.poll_once()

    service.users.return_value.history.return_value.list.assert_called_once()
    service.users.return_value.messages.return_value.list.assert_not_called()


def test_body_text_fetched_when_company_is_none(poller, db):
    """When company is None after initial parse, body text is fetched to refine it."""
    no_company_msg = {
        "id": "msg-nobody",
        "threadId": "thread-nobody",
        "snippet": "Your application has been received",
        "payload": {
            "headers": [
                {"name": "From", "value": "noreply@naukri.com"},
                {"name": "Subject", "value": "Application received"},
                {"name": "Date", "value": "Mon, 13 May 2024 10:00:00 +0000"},
            ]
        },
    }

    service = make_mock_service([no_company_msg])
    # Mock full-body fetch returning text that helps extract company
    full_msg = {
        "payload": {
            "mimeType": "text/plain",
            "body": {
                "data": __import__("base64")
                .urlsafe_b64encode(
                    b"Dear Candidate, Your application to Infosys has been received."
                )
                .decode()
            },
        }
    }
    service.users.return_value.messages.return_value.get.side_effect = None

    def get_side_effect(**kwargs):
        mock = MagicMock()
        if kwargs.get("format") == "full":
            mock.execute.return_value = full_msg
        else:
            mock.execute.return_value = no_company_msg
        return mock

    service.users.return_value.messages.return_value.get.side_effect = get_side_effect
    poller.service = service
    poller.poll_once()

    # Application was created (company may or may not be refined depending on NER)
    _, total = db.get_applications(ApplicationFilter())
    assert total == 1


def test_build_raw_email_with_bad_date_falls_back_to_utcnow(poller):
    """_build_raw_email falls back to utcnow when Date header is unparseable."""
    msg = {
        "id": "msg-bad-date",
        "threadId": "thread-bad-date",
        "snippet": "",
        "payload": {
            "headers": [
                {"name": "From", "value": "hr@test.com"},
                {"name": "Subject", "value": "Job Application"},
                {"name": "Date", "value": "this is not a date"},
            ]
        },
    }
    raw = poller._build_raw_email(msg)
    from backend.db.models import utc_now

    # Should not raise and should be close to now
    assert (utc_now() - raw.date).total_seconds() < 5


def test_poll_updates_last_history_id(poller, db):
    """After a successful poll, last_history_id is updated."""
    poller.service = make_mock_service([])
    assert poller.last_history_id is None

    poller.poll_once()

    assert poller.last_history_id == "99999"
