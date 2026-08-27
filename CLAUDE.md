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
- Status transitions only via StatusUpdater._advance_status(). No direct status field writes.
- Config values (paths, ports, thresholds) only from config.py. No hardcoded values.
- No email body text stored in DB. Only: sender, subject, date, extracted fields, snippet.
- Every public method must have type hints. No bare `except:` — always catch specific exceptions.

## File layout
- backend/config.py — all paths and constants
- backend/db/models.py — SQLModel table definitions (source of truth for schema)
- backend/parser/portal_rules.yaml — user-editable portal detection rules
- backend/api/routes.py — all FastAPI endpoints

## Test conventions
- Fixtures in tests/conftest.py
- Use tmp_path for any file I/O in tests
- Mock Gmail API with unittest.mock — never call real API in tests
- Each test file mirrors the source file: tests/unit/test_email_parser.py → backend/parser/email_parser.py

## Do not
- Do not use print() for logging. Use structlog.
- Do not create new config files. Use config.py.
- Do not write raw SQL strings. Use SQLModel ORM methods. Two documented exceptions, both in `data_store.py`: (1) `DataStore._migrate_schema()` uses `text("ALTER TABLE ...")` — SQLAlchemy has no portable Core/ORM construct for `ALTER TABLE ADD COLUMN`. (2) `DataStore.get_raw_status_values()` uses `text("SELECT DISTINCT ...")` to read `current_status` bypassing the SAEnum column's result-level coercion — a Core `select()` on the same mapped column still applies that coercion, so only a genuinely raw string skips it, which is the point (detecting legacy NAME-format corruption without the read itself raising `LookupError`). Neither is used for ordinary querying/writing of rows.
- Do not add new dependencies without updating requirements.txt.
- CLI-only scripts (`diagnostics.py`, `poll_once_cli.py`, `reset_for_rebackfill.py`, `import_from_excel.py`) may use `print()` for their human-facing report/progress output — that's their actual UI, not application logging. Anything logged for operational/debugging purposes, in these scripts or elsewhere, still goes through structlog.
