"""Gmail API polling logic."""

from enum import Enum
import json
import base64
import threading
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import os

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
from backend.poller.error_retry import gmail_retry, AuthError, StaleHistoryError

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

    def authenticate_headless(self) -> bool:
        """Authenticate from the stored keychain/env token only — never opens a browser.

        For headless/cron use (poll_once_cli.py). Returns False if there's no valid,
        refreshable token, in which case the caller should direct the operator to run
        the interactive `authenticate()` flow once via setup_wizard.py.
        """
        creds = self._load_token_from_keyring()
        if creds is None:
            return False

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token_to_keyring(creds)
            except Exception as exc:
                log.error("token_refresh_failed", error=str(exc))
                return False

        if not creds.valid:
            return False

        self.service = build("gmail", "v1", credentials=creds)
        log.info("gmail_authenticated_headless")
        return True

    def _load_token_from_keyring(self) -> Credentials | None:
        token_json = os.environ.get("GMAIL_TOKEN_JSON") or keyring.get_password(
            GMAIL_KEYCHAIN_SERVICE, GMAIL_KEYCHAIN_USERNAME
        )
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
        if os.environ.get("GMAIL_TOKEN_JSON"):
            # Fly.io secrets are read-only at runtime; refreshed token stays
            # in-memory for this process and re-refreshes on next restart.
            log.info("gmail_token_refresh_not_persisted_env_backed")
            return
        keyring.set_password(
            GMAIL_KEYCHAIN_SERVICE, GMAIL_KEYCHAIN_USERNAME, creds.to_json()
        )

    @property
    def is_polling(self) -> bool:
        return self._poll_lock.locked()

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
        new_count = 0
        update_count = 0

        try:
            if self.last_history_id is not None:
                try:
                    message_ids, new_history_id = self._fetch_via_history()
                except StaleHistoryError:
                    log.warning("history_id_stale_resetting_to_backfill")
                    self.last_history_id = None
                    message_ids, new_history_id = self._fetch_backfill()
            else:
                message_ids, new_history_id = self._fetch_backfill()

            for msg_id, thread_id in message_ids:
                if self._db.is_processed(msg_id):
                    continue

                outcome = self._process_message(msg_id, suppress_rules)
                if outcome == "new":
                    new_count += 1
                elif outcome == "update":
                    update_count += 1

            if new_history_id:
                self.last_history_id = new_history_id

            self._db.update_poller_state(
                status=PollerStatus.RUNNING.value,
                last_history_id=self.last_history_id,
                last_sync_at=datetime.utcnow(),
                clear_error=True,
            )
            self.status = PollerStatus.RUNNING
            log.info(
                "poll_cycle_complete",
                new_applications=new_count,
                status_updates=update_count,
            )
            return new_count

        except HttpError as e:
            if e.resp.status in (401, 403):
                self._db.update_poller_state(
                    status=PollerStatus.AUTH_ERROR.value,
                    error_message=str(e),
                )
                self.status = PollerStatus.AUTH_ERROR
                raise AuthError(f"Gmail auth failed: {e.resp.status}") from e
            self._db.update_poller_state(
                status=PollerStatus.API_ERROR.value,
                error_message=f"HTTP {e.resp.status}: {str(e)[:200]}",
            )
            self.status = PollerStatus.API_ERROR
            raise

    def _process_message(self, msg_id: str, suppress_rules: list["SuppressRule"]) -> str:
        """Fetch, parse, and persist a single message. Returns "new", "update",
        "suppressed", "not_found", or "error"."""
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            raw_email = self._build_raw_email(msg)
            parsed = self._parser.parse(raw_email, suppress_rules)

            if parsed is None:
                self._db.mark_processed(msg_id, "suppressed")
                return "suppressed"

            if parsed.company is None or parsed.role is None:
                body = self._fetch_body_text(msg_id)
                if body:
                    if parsed.company is None:
                        refined_company = self._parser.refine_company(
                            body,
                            parsed.source_portal,
                            extract_sender_domain(raw_email.sender),
                        )
                        if refined_company:
                            parsed = dataclass_replace(parsed, company=refined_company)
                    if parsed.role is None:
                        refined_role = self._parser.refine_role(raw_email.subject, body)
                        if refined_role:
                            parsed = dataclass_replace(parsed, role=refined_role)

            _, is_new = self._updater.process(parsed)
            return "new" if is_new else "update"
        except HttpError as e:
            if e.resp.status == 404:
                # Message deleted/inaccessible on Gmail's side since it was listed;
                # skip it rather than aborting the whole poll cycle.
                log.warning("message_not_found_skipping", message_id=msg_id)
                self._db.mark_processed(msg_id, "not_found")
                return "not_found"
            raise  # let the outer handler deal with auth/rate errors
        except Exception as exc:
            log.error("email_processing_error_skipping", message_id=msg_id, error=str(exc))
            return "error"

    def get_thread_sender_domain(self, thread_id: str, timeout: int = 10) -> str | None:
        """Fetch a thread's first message From header and return its sender domain.

        Runs the blocking Gmail call in a worker thread with a bounded timeout so a
        slow/hanging Gmail API can't stall the caller indefinitely. Returns None if the
        thread has no messages, the sender domain can't be extracted, or the call times out.
        """
        if self.service is None:
            return None
        import concurrent.futures

        request_obj = self.service.users().threads().get(
            userId="me", id=thread_id,
            format="metadata", metadataHeaders=["From"],
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(request_obj.execute)
            try:
                thread = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                log.warning("gmail_thread_fetch_timeout", thread_id=thread_id)
                return None

        messages = thread.get("messages", [])
        if not messages:
            return None
        headers = {
            h["name"]: h["value"]
            for h in messages[0].get("payload", {}).get("headers", [])
        }
        return extract_sender_domain(headers.get("From", ""))

    def backfill_portal(self, sender_domains: list[str], days: int = BACKFILL_DAYS) -> dict[str, int]:
        """Re-scan Gmail for a portal's domains and pick up applications missed
        before a portal_rules.yaml entry existed for it.

        Existing applications whose thread already exists in the DB (likely
        misclassified under Direct/Unknown) get their source_portal corrected.
        Threads with no existing application get processed as new.
        """
        suppress_rules = self._db.get_suppress_rules()
        domain_query = " OR ".join(f"from:{d}" for d in sender_domains)
        q = f"({domain_query}) newer_than:{days}d"

        message_ids: list[tuple[str, str]] = []
        page_token: str | None = None
        while True:
            kwargs: dict = {"userId": "me", "q": q, "maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            response = self.service.users().messages().list(**kwargs).execute()
            for m in response.get("messages", []):
                message_ids.append((m["id"], m["threadId"]))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        reclassified = 0
        created = 0
        skipped = 0
        portal_name = sender_domains[0]
        for portal in self._parser._portals:  # noqa: SLF001 — read-only lookup
            if any(d in portal.get("sender_domains", []) for d in sender_domains):
                portal_name = portal["name"]
                break

        for msg_id, thread_id in message_ids:
            existing = self._db.find_application_by_thread_id(thread_id)
            if existing is not None:
                if existing.source_portal != portal_name:
                    existing.source_portal = portal_name
                    if existing.company is None or existing.role is None:
                        company, role = self.reextract_fields(existing)
                        if company:
                            existing.company = company
                        if role:
                            existing.role = role
                    self._db.upsert_application(existing)
                    reclassified += 1
                else:
                    skipped += 1
                continue

            if self._db.is_processed(msg_id):
                # Previously seen with no matching portal rule (e.g. suppressed
                # or fell through to Direct/Consultancy) — clear the marker so
                # it goes through processing again now that a rule covers it.
                self._db.clear_processed(msg_id)

            outcome = self._process_message(msg_id, suppress_rules)
            if outcome == "new":
                created += 1
            else:
                skipped += 1

        log.info(
            "portal_backfill_complete",
            portal=portal_name,
            reclassified=reclassified,
            created=created,
            skipped=skipped,
        )
        return {"reclassified": reclassified, "created": created, "skipped": skipped}

    def reextract_fields(self, app: "Application") -> tuple[str | None, str | None]:
        """Re-fetch an application's first thread from Gmail and re-run field extraction."""
        from backend.db.models import Application  # local import to avoid module-level cycle
        thread_ids_raw: list[str] = json.loads(app.thread_ids)
        if not thread_ids_raw:
            return None, None

        try:
            thread = self.service.users().threads().get(
                userId="me",
                id=thread_ids_raw[0],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError as exc:
            log.warning("reextract_thread_fetch_failed", thread_id=thread_ids_raw[0], error=str(exc))
            return None, None

        messages = thread.get("messages", [])
        if not messages:
            return None, None

        msg = messages[0]
        raw_email = self._build_raw_email(msg)
        body = self._fetch_body_text(msg["id"])

        return self._parser.extract_fields(
            sender=raw_email.sender,
            subject=raw_email.subject,
            snippet=raw_email.snippet,
            body_text=body or None,
            source_portal=app.source_portal,
        )

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

            try:
                response = self.service.users().history().list(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    raise StaleHistoryError("History ID expired") from e
                raise
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
            parsed_date = parsedate_to_datetime(date_str)
            if parsed_date.tzinfo is not None:
                parsed_date = parsed_date.astimezone(timezone.utc)
            date = parsed_date.replace(tzinfo=None)
        except Exception as exc:
            log.warning("email_date_parse_failed", date_header=date_str, error=str(exc))
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
