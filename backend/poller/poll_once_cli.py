"""
One-shot Gmail poll for headless / cron use.

Authenticates from the macOS Keychain (OAuth token stored by setup_wizard.py).
Does NOT open a browser — if credentials are missing or expired and can't be
refreshed, exits with code 2 so the caller can alert.

Usage:
    python -m backend.poller.poll_once_cli          # poll and exit
    python -m backend.poller.poll_once_cli --dry-run # auth check only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import keyring
import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as _build
from googleapiclient.errors import HttpError

from backend.config import (
    GMAIL_KEYCHAIN_SERVICE,
    GMAIL_KEYCHAIN_USERNAME,
    GMAIL_SCOPES,
)
from backend.poller.error_retry import AuthError
from backend.poller.scheduler import build_poller

log = structlog.get_logger(__name__)


def _load_headless_credentials() -> Credentials | None:
    """Load stored OAuth token from keychain without opening a browser."""
    token_json = keyring.get_password(GMAIL_KEYCHAIN_SERVICE, GMAIL_KEYCHAIN_USERNAME)
    if not token_json:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), GMAIL_SCOPES)
    except Exception as exc:
        log.error("keyring_token_corrupt", error=str(exc))
        return None
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token back to keychain
            keyring.set_password(GMAIL_KEYCHAIN_SERVICE, GMAIL_KEYCHAIN_USERNAME, creds.to_json())
        except Exception as exc:
            log.error("token_refresh_failed", error=str(exc))
            return None
    return creds if creds.valid else None


def poll_once(dry_run: bool = False) -> int:
    """
    Authenticate headlessly and run one poll cycle.

    Returns the number of new applications processed.
    Raises SystemExit with code 2 on auth failure, code 1 on API error.
    """
    creds = _load_headless_credentials()
    if creds is None:
        log.error(
            "no_valid_credentials",
            hint="Run: python backend/setup_wizard.py  to complete initial OAuth setup",
        )
        sys.exit(2)

    if dry_run:
        log.info("dry_run_auth_ok")
        print("Auth OK — credentials are valid.")
        return 0

    poller = build_poller()
    # Inject headless credentials directly instead of running the interactive flow
    poller.service = _build("gmail", "v1", credentials=creds)

    started_at = datetime.now(timezone.utc)
    try:
        count = poller.poll_once()
    except AuthError as exc:
        log.error("poll_auth_error", error=str(exc))
        sys.exit(2)
    except HttpError as exc:
        log.error("poll_http_error", error=str(exc))
        sys.exit(1)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    log.info("poll_complete", new_applications=count, elapsed_seconds=round(elapsed, 1))
    print(f"Poll complete — {count} new application(s) processed in {elapsed:.1f}s")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot Gmail poll for job-tracker")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify credentials only; do not fetch emails",
    )
    args = parser.parse_args()
    poll_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
