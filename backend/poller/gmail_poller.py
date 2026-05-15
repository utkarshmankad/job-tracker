"""Gmail API polling logic."""

from enum import Enum
import json
import base64
import threading
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import keyring
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import structlog

from backend.config import (
    BACKFILL_DAYS,
    CREDENTIALS_PATH,
    GMAIL_KEYCHAIN_SERVICE,
    GMAIL_KEYCHAIN_USERNAME,
    GMAIL_SCOPES,
)
from backend.db.data_store import DataStore
from backend.parser.email_parser import EmailParser, RawEmail, extract_sender_domain
from backend.engine.status_updater import StatusUpdater
from backend.poller.error_retry import gmail_retry, AuthError

log = structlog.get_logger()


class PollerStatus(str, Enum):
    RUNNING = "RUNNING"
    AUTH_ERROR = "AUTH_ERROR"
    API_ERROR = "API_ERROR"
    SLEEPING = "SLEEPING"


class GmailPoller:
    def __init__(self, db: DataStore, parser: EmailParser, updater: StatusUpdater) -> None:
        self._db = db
        self._parser = parser
        self._updater = updater
        self._poll_lock = threading.Lock()
        self.service = None
        self.status = PollerStatus.SLEEPING
        state = db.get_poller_state()
        self.last_history_id: str | None = state.last_history_id

    def authenticate(self) -> None:
        creds = self._load_token_from_keyring()

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save_token_to_keyring(creds)

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
            self._save_token_to_keyring(creds)

        self.service = build("gmail", "v1", credentials=creds)
        log.info("gmail_authenticated")

    def _load_token_from_keyring(self) -> Credentials | None:
        token_json = keyring.get_password(GMAIL_KEYCHAIN_SERVICE, GMAIL_KEYCHAIN_USERNAME)
        if not token_json:
            return None
        try:
            return Credentials.from_authorized_user_info(
                json.loads(token_json), GMAIL_SCOPES
            )
        except Exception as exc:
            log.warning("keyring_token_invalid", error=str(exc))
            return None

    def _save_token_to_keyring(self, creds: Credentials) -> None:
        keyring.set_password(
            GMAIL_KEYCHAIN_SERVICE, GMAIL_KEYCHAIN_USERNAME, creds.to_json()
        )

    @gmail_retry()
    def poll_once(self) -> int:
        if not self._poll_lock.acquire(blocking=False):
            log.info("poll_skipped_already_running")
            return 0
        try:
            return self._poll_once_locked()
        finally:
            self._poll_lock.release()

    def _poll_once_locked(self) -> int:
        suppress_rules = self._db.get_suppress_rules()
        processed_count = 0

        try:
            if self.last_history_id is not None:
                message_ids, new_history_id = self._fetch_via_history()
            else:
                message_ids, new_history_id = self._fetch_backfill()

            for msg_id, thread_id in message_ids:
                if self._db.is_processed(msg_id):
                    continue

                msg = self.service.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()

                raw_email = self._build_raw_email(msg)
                parsed = self._parser.parse(raw_email, suppress_rules)

                if parsed is not None:
                    if parsed.company is None:
                        body = self._fetch_body_text(msg_id)
                        if body:
                            refined = self._parser.refine_company(
                                body,
                                parsed.source_portal,
                                extract_sender_domain(raw_email.sender),
                            )
                            if refined:
                                parsed = dataclass_replace(parsed, company=refined)
                    self._updater.process(parsed)
                    processed_count += 1
                else:
                    self._db.mark_processed(msg_id, "suppressed")

            if new_history_id:
                self.last_history_id = new_history_id

            self._db.update_poller_state(
                status=PollerStatus.RUNNING.value,
                last_history_id=self.last_history_id,
                last_sync_at=datetime.now(timezone.utc),
            )
            self.status = PollerStatus.RUNNING
            return processed_count

        except HttpError as e:
            if e.resp.status in (401, 403):
                self._db.update_poller_state(
                    status=PollerStatus.AUTH_ERROR.value,
                    error_message=str(e),
                )
                self.status = PollerStatus.AUTH_ERROR
                raise AuthError(f"Gmail auth failed: {e.resp.status}") from e
            raise

    def _fetch_backfill(self) -> tuple[list[tuple[str, str]], str | None]:
        q = f"newer_than:{BACKFILL_DAYS}d"
        message_ids: list[tuple[str, str]] = []
        page_token: str | None = None
        new_history_id: str | None = None

        while True:
            kwargs: dict = {"userId": "me", "q": q, "maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token

            response = self.service.users().messages().list(**kwargs).execute()

            if new_history_id is None:
                new_history_id = response.get("historyId")

            for m in response.get("messages", []):
                message_ids.append((m["id"], m["threadId"]))

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return message_ids, new_history_id

    def _fetch_via_history(self) -> tuple[list[tuple[str, str]], str | None]:
        message_ids: list[tuple[str, str]] = []
        page_token: str | None = None
        new_history_id: str | None = None

        while True:
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": self.last_history_id,
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token

            response = self.service.users().history().list(**kwargs).execute()
            new_history_id = response.get("historyId")

            for history_item in response.get("history", []):
                for msg_added in history_item.get("messagesAdded", []):
                    m = msg_added.get("message", {})
                    if "id" in m and "threadId" in m:
                        message_ids.append((m["id"], m["threadId"]))

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return message_ids, new_history_id

    def _fetch_body_text(self, msg_id: str) -> str:
        """Fetch full message and extract plain-text body. Body is never stored."""
        try:
            msg = self.service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
            return self._extract_text_from_payload(msg.get("payload", {}))
        except HttpError:
            return ""

    def _extract_text_from_payload(self, payload: dict) -> str:
        """Recursively decode the first text/plain part from a MIME payload."""
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                try:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                except Exception as exc:
                    log.warning("body_decode_error", error=str(exc))
                    return ""
        for part in payload.get("parts", []):
            text = self._extract_text_from_payload(part)
            if text:
                return text
        return ""

    def _build_raw_email(self, msg: dict) -> RawEmail:
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")
        date_str = headers.get("Date", "")

        try:
            date = parsedate_to_datetime(date_str).replace(tzinfo=None)
        except Exception:
            date = datetime.utcnow()

        return RawEmail(
            message_id=msg["id"],
            thread_id=msg["threadId"],
            sender=sender,
            subject=subject,
            date=date,
            snippet=msg.get("snippet", ""),
            body_text=None,
        )
