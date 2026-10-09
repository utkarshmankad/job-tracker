# Job Tracker — Claude Code Context

## Project
Local Mac app. Python 3.11. FastAPI backend on jobtracker.localhost:8000. React frontend on jobtracker.localhost:5173. SQLite at ~/Codes/job-tracker/.job-tracker/applications.db. Gmail API read-only polling every 5 minutes.

## Commands
- Run backend: `cd backend && uvicorn main:app --reload --port 8000`
- Run tests: `pytest tests/ -v`
- Run single test: `pytest tests/path/to/test.py::test_name -v`
- Lint: `ruff check backend/ tests/`
- Format: `ruff format backend/ tests/`
- Type check: `mypy backend/`

## Architecture rules (NEVER violate)
- All DB access goes through DataStore class only. No raw sqlite3 calls outside data_store.py.
- All Gmail API calls go through GmailPoller only. No direct google-api calls elsewhere.
- Status transitions only via StatusUpdater._advance_status(). No direct status field writes. One documented exception: `DataStore.execute_merge()`/`undo_merge()` set the survivor's `current_status` to the value the user explicitly chose among the merged records (or restore it from the snapshot on undo). This is audited in `mergeoperation` and reversible; see docs/phase-2-identity-resolution.md §12.
- Config values (paths, ports, thresholds) only from config.py. No hardcoded values.
- Schema changes only via Alembic revisions in `backend/db/alembic/versions/` (see `docs/database-operations.md`). Never `create_all()` or runtime `ALTER TABLE`. Revisions are frozen: they must not import `backend/db/models.py`.
- Never copy the live SQLite file; backups go through `scripts/backup_database.py` (SQLite online backup API).
- No email body text stored in DB. Only: sender, subject, date, extracted fields, snippet.
- Every received item (Gmail message, portal import, …) is stored once as `Evidence` via `DataStore.insert_evidence` (idempotent: unique fingerprint and `(source, external_id)`). Only an acknowledgement with no matching application may create one; follow-up/status mail that matches nothing is left `needs_review`. Identity is decided by the deterministic scoring resolver in `backend/engine/identity_resolver.py` (weights/thresholds in config.py); normalization lives only in `backend/engine/normalization.py`. Human decisions (`decided_by=human`) are never overwritten by automated processing. Non-job mail is stored minimally (IDs and date only). See `docs/phase-2-identity-resolution.md`.
- Every public method must have type hints. No bare `except:` — always catch specific exceptions.

## Source collection (Phase 3) rules (NEVER violate)
- The collector is read-only: it never submits, withdraws, messages, edits profiles, accepts anything or bypasses CAPTCHAs/challenges. No stealth plugins, fingerprint spoofing, proxy rotation, solvers or private APIs.
- Collector credentials authenticate only `collector_router` endpoints; every other endpoint stays session-cookie + CSRF. New routes must be classified in tests/integration/test_auth.py.
- Collected observations never auto-merge applications and never create applications unless extraction is `verified` (adapter `LIVE_VERIFIED` set by a person). See docs/phase-3-source-collection.md.
- Collector fixtures are synthetic only — never commit a real page, cookie, profile or account identifier.

## File layout
- backend/config.py — all paths and constants
- backend/db/models.py — SQLModel table definitions (source of truth for schema)
- backend/parser/portal_rules.yaml — user-editable portal detection rules
- backend/api/routes.py — all FastAPI endpoints

## Test conventions
- Test isolation is automatic: `tests/isolation.py` (installed first by tests/conftest.py, before any backend import) sets a temporary `JOB_TRACKER_DIR`, `POLLER_ENABLED=false`, `CACHE_ENABLED=false`, `LLM_ENABLED=false`, strips Gmail/Groq/auth secrets, skips `.env`, and installs keyring and socket guards. Any test that reaches the keychain or a non-loopback address fails, even if the code swallowed the error. Never import backend modules in conftest above `isolation.install()`.
- Gmail, keyring and HTTP behaviour are tested only with mocks and synthetic messages. Route tests get a fake scheduler (`fake_poller_scheduler`); the real poller never starts in tests.
- Fixtures in tests/conftest.py
- Use tmp_path for any file I/O in tests
- Mock Gmail API with unittest.mock — never call real API in tests
- Each test file mirrors the source file: tests/unit/test_email_parser.py → backend/parser/email_parser.py

## Do not
- Do not use print() for logging. Use structlog.
- Do not create new config files. Use config.py.
- Do not write raw SQL strings. Use SQLModel ORM methods. Two documented exceptions, both in `data_store.py`: (1) the backup primitives (`DataStore.online_backup`, `DataStore.integrity_check`) use the `sqlite3` module and `PRAGMA` statements — SQLite's online backup API and integrity pragmas have no ORM equivalent. (2) `DataStore.get_raw_status_values()` uses `text("SELECT DISTINCT ...")` to read `current_status` bypassing the SAEnum column's result-level coercion — a Core `select()` on the same mapped column still applies that coercion, so only a genuinely raw string skips it, which is the point (detecting legacy NAME-format corruption without the read itself raising `LookupError`). Neither is used for ordinary querying/writing of rows.
- Do not add new dependencies without updating requirements.txt. Exception: dependencies of the local collection agent (`collector/`, never deployed) go in `requirements-collector.txt`.
- CLI-only scripts (`diagnostics.py`, `poll_once_cli.py`, `reset_for_rebackfill.py`, `import_from_excel.py`, and `backend/db/recovery_cli.py` via `click.echo`) may use `print()` for their human-facing report/progress output — that's their actual UI, not application logging. Anything logged for operational/debugging purposes, in these scripts or elsewhere, still goes through structlog.
