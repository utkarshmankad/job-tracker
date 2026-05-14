"""Integration tests for GmailPoller — Gmail API is fully mocked."""

import pytest
from unittest.mock import MagicMock

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
    list_execute = (
        service.users.return_value.messages.return_value.list.return_value.execute
    )
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
