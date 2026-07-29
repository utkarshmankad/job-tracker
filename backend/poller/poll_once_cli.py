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
import sys
from datetime import datetime, timezone

import structlog
from googleapiclient.errors import HttpError

from backend.poller.error_retry import AuthError
from backend.poller.scheduler import build_poller

log = structlog.get_logger(__name__)


def poll_once(dry_run: bool = False) -> int:
    """
    Authenticate headlessly and run one poll cycle.

    Returns the number of new applications processed.
    Raises SystemExit with code 2 on auth failure, code 1 on API error.
    """
    poller = build_poller()
    if not poller.authenticate_headless():
        log.error(
            "no_valid_credentials",
            hint="Run: python backend/setup_wizard.py  to complete initial OAuth setup",
        )
        sys.exit(2)

    if dry_run:
        log.info("dry_run_auth_ok")
        print("Auth OK — credentials are valid.")
        return 0

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
    # new_applications count comes from _poll_once_locked which already logs it;
    # this line records the elapsed time for the CLI invocation.
    log.info("poll_once_cli_complete", new_applications=count, elapsed_seconds=round(elapsed, 1))
    print(f"Poll complete — {count} new application(s) in {elapsed:.1f}s")
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
