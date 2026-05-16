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
- Do not write raw SQL strings. Use SQLModel ORM methods.
- Do not add new dependencies without updating requirements.txt.
