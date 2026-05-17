# Job Tracker

> *Because copy-pasting job statuses into a spreadsheet at 11 PM is a form of self-harm.*

A fully local Mac app that reads your Gmail, figures out where you are in every job application pipeline, and surfaces the whole thing in a slick dashboard — no SaaS subscriptions, no data leaving your machine, no VC-funded "free tier" with a 100-application limit.

---

## What it actually does

- **Polls Gmail every 5 minutes** (read-only OAuth, never touches anything) and looks for job-related emails
- **Parses them with NLP** (spaCy + YAML rules) to extract company names, portals, and status signals — now crash-proof with graceful fallback on malformed emails
- **Advances application status automatically** through a defined pipeline: `Applied → In Review → Phone Screen → Interview → Offer → Rejected`
- **Detects duplicates** so one application doesn't show up twelve times because Workday sends a new email every time someone breathes
- **Surfaces analytics**: Sankey flow diagram, funnel donut, KPI cards, conversion rates
- **Icon-driven UI** — every action button is a Lucide icon with proper `aria-label`, search has an inline icon, pagination uses chevrons, sort columns show directional arrows
- **Dark mode** because your eyes matter
- **Sleep/wake aware** — pauses polling when your Mac is asleep, resumes when it wakes up (macOS `IOPMLib` integration)
- **Diagnostic utility** — runs a suite of health checks on startup and exposes them at `/api/v1/diagnostics`; catches DB enum-format issues, schema drift, and stale poller state before they become runtime errors
- **One-shot poll CLI** — run `python -m backend.poller.poll_once_cli` to trigger a single Gmail sync from the terminal (useful for scripting and debugging)
- **AI agent layer** — `src/ai/` wires up an Anthropic-powered agent with MCP servers for Gmail, Google Drive, and Notion
- Runs as **launchd daemons** so it's always on in the background

### Supported job portals

Detected automatically from email sender domains and subject-line patterns:

Greenhouse, Workday, Lever, iCIMS, SmartRecruiters, Taleo, Ashby, Workable, BreezyHR, LinkedIn, Indeed, Naukri, Foundit, TimesJobs, Snaphunt — plus a `Direct` catch-all for recruiter emails and a configurable `Unknown` fallback.

---

## Tech stack

### Backend
| Thing | What it does |
|---|---|
| **Python 3.11** | Core runtime |
| **FastAPI** | REST API on `jobtracker.localhost:8000` |
| **SQLModel + SQLite** | ORM + local DB at `~/.job-tracker/applications.db` |
| **spaCy** (`en_core_web_sm`) | NLP for company/role extraction from email text |
| **threading.Event scheduler** | Custom daemon-thread poller (replaced APScheduler) — wakes on timer, Mac wake events, or manual trigger |
| **google-api-python-client** | Gmail API (read-only) |
| **structlog** | Structured JSON logging |
| **tenacity** | Retry logic with exponential backoff |
| **rapidfuzz** | Fuzzy deduplication of company names |
| **pyobjc-framework-Cocoa** | Hooks into macOS sleep/wake notifications |
| **anthropic** | AI agent layer via Anthropic API + MCP |

### Frontend
| Thing | What it does |
|---|---|
| **React 19** | UI framework |
| **Vite 8** | Build tool / dev server on `jobtracker.localhost:5173` |
| **Tailwind CSS 4** | Styling |
| **Recharts** | Donut and KPI charts |
| **d3-sankey** | Sankey flow diagram in the analytics panel |
| **TanStack Table** | Applications table with sorting/filtering |
| **Lucide React** | Icon set used throughout the UI |

---

## Project structure

```
job-tracker/
├── backend/
│   ├── api/routes.py          # All FastAPI endpoints
│   ├── config.py              # Every path, port, and constant lives here
│   ├── diagnostics.py         # DiagnosticRunner — health checks exposed at /api/v1/diagnostics
│   ├── db/
│   │   ├── models.py          # SQLModel table definitions (schema source of truth)
│   │   └── data_store.py      # All DB access — nothing talks to SQLite directly
│   ├── engine/
│   │   ├── status_updater.py  # Status transitions (only place allowed to advance status)
│   │   ├── insights_engine.py # Analytics computations
│   │   └── duplicate_detector.py
│   ├── parser/
│   │   ├── email_parser.py    # NLP-based email parsing (crash-proof)
│   │   ├── status_signals.py  # Pattern matching for status extraction
│   │   └── portal_rules.yaml  # Edit this to add new job portals — no code needed
│   ├── poller/
│   │   ├── gmail_poller.py    # Gmail API polling
│   │   ├── scheduler.py       # Custom threading.Event-based scheduler
│   │   ├── poll_once_cli.py   # One-shot poll CLI: python -m backend.poller.poll_once_cli
│   │   ├── error_retry.py     # AuthError, StaleHistoryError, retry helpers
│   │   └── sleep_watcher.py   # macOS sleep/wake integration
│   ├── main.py                # FastAPI app + lifespan (starts scheduler, exposes state)
│   └── setup_wizard.py        # One-time OAuth + launchd setup
├── frontend/
│   ├── public/favicon.svg     # Custom briefcase+checkmark logo
│   └── src/
│       ├── components/        # ApplicationsTable, AnalyticsPanel, StatusPage, Filters, etc.
│       └── contexts/          # ThemeContext (dark mode)
├── src/ai/
│   ├── agent.py               # Anthropic-powered agent entry point
│   ├── mcp_config.py          # MCP server wiring (Gmail, Google Drive, Notion)
│   ├── prompts.py             # System prompts
│   └── exceptions.py          # Agent-specific exception types
├── tests/
│   ├── unit/                  # 9 modules, 167 tests
│   │   ├── test_diagnostics.py
│   │   ├── test_email_parser.py
│   │   ├── test_status_updater.py
│   │   ├── test_insights_engine.py
│   │   ├── test_poll_once_cli.py
│   │   └── ...
│   ├── integration/           # 33 route tests covering all API endpoints
│   │   └── test_routes.py
│   └── e2e/                   # Playwright end-to-end tests
└── start.sh                   # Quick launcher for both services
```

---

## Setup

### Prerequisites

- macOS 13+
- Python 3.11+
- Node.js 18+ (for the frontend)
- A Gmail account that receives your job application emails
- ~15 minutes for Google Cloud OAuth setup (one-time)

### 1. Clone and install

```bash
git clone https://github.com/utkarshmankad/job-tracker.git
cd job-tracker

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm

cd frontend && npm install && cd ..
```

### 2. Google Cloud OAuth setup (the annoying-but-necessary bit)

You need a Gmail API credential. This is free and never expires after you do it once.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a new project
2. **APIs & Services → Library** → search "Gmail API" → Enable
3. **APIs & Services → OAuth consent screen** → External → add your Gmail as a test user → scope: `gmail.readonly`
   - Hit **Publish App** to avoid the 7-day token expiry on "Testing" apps
4. **APIs & Services → Credentials** → Create → OAuth 2.0 Client ID → Desktop app → Download JSON
5. Move it into place:
   ```bash
   mv ~/Downloads/client_secret_*.json ~/.job-tracker/client_secret.json
   ```

### 3. Run the setup wizard

```bash
source .venv/bin/activate
python backend/setup_wizard.py setup
```

This will:
- Open your browser for Gmail OAuth (click through the "unverified app" warning — it's your own app)
- Create the SQLite database
- Register and start two launchd daemons: `com.jobtracker.api` and `com.jobtracker.poller`

### 4. Start the frontend

```bash
# Either use the convenience script:
./start.sh

# Or manually:
source .venv/bin/activate
cd frontend && npm run dev
```

Open [http://jobtracker.localhost:5173](http://jobtracker.localhost:5173) — you're in.

---

## Useful commands

```bash
# Stop/start the background daemons
launchctl stop com.jobtracker.api
launchctl stop com.jobtracker.poller
launchctl start com.jobtracker.api
launchctl start com.jobtracker.poller

# Re-authenticate if your OAuth token expires
python backend/setup_wizard.py reauth

# Trigger a one-shot Gmail poll from the terminal
python -m backend.poller.poll_once_cli

# Run diagnostics to check DB health, enum format, and poller state
python -m backend.diagnostics
# Or hit the API endpoint:
curl http://jobtracker.localhost:8000/api/v1/diagnostics | jq

# Watch the logs
tail -f ~/.job-tracker/logs/poller.log
tail -f ~/.job-tracker/logs/api_error.log

# Run the test suite
pytest tests/ -v --ignore=tests/e2e        # unit + integration (200 tests)
pytest tests/e2e/ -v                       # Playwright end-to-end

# Coverage
pytest tests/ --cov=backend --ignore=tests/e2e

# Lint, format, type check
ruff check backend/ tests/
ruff format backend/ tests/
mypy backend/
```

---

## Adding a new job portal

Don't touch Python — just edit `backend/parser/portal_rules.yaml`. Each portal entry looks like:

```yaml
- name: Greenhouse
  domains: ["greenhouse.io", "boards.greenhouse.io"]
  signals:
    applied: ["application received", "thanks for applying"]
    rejected: ["not moving forward", "decided to pursue other candidates"]
```

Add your portal, restart the poller (`launchctl stop/start com.jobtracker.poller`), done.

---

## Diagnostic endpoint

`GET /api/v1/diagnostics` returns a JSON report of backend health:

```json
{
  "passed": 5,
  "failed": 0,
  "checked_at": "2026-05-18T01:00:00",
  "results": [
    { "name": "db_file",        "ok": true,  "detail": "Exists (2.4 MB)" },
    { "name": "db_connectivity","ok": true,  "detail": "Connected" },
    { "name": "schema_tables",  "ok": true,  "detail": "All tables present" },
    { "name": "enum_values",    "ok": true,  "detail": "3 distinct values, all valid" },
    { "name": "poller_state",   "ok": true,  "detail": "SLEEPING, last sync 2 min ago" }
  ]
}
```

The `enum_values` check is the critical one: it detects old databases that stored enum member names (`"APPLIED"`) instead of values (`"Applied"`) — a format mismatch that causes `LookupError` on every read after the SQLAlchemy migration.

You can also manually trigger a poll without waiting for the 5-minute interval:

```bash
curl -X POST http://jobtracker.localhost:8000/api/v1/poller/trigger
```

---

## Contributing

PRs welcome. Here's how to not get your PR closed immediately:

### Rules of the road

- **All DB access via `DataStore`** — no raw SQLite calls anywhere else
- **All Gmail calls via `GmailPoller`** — no direct `google-api` calls outside it
- **Status transitions via `StatusUpdater._advance_status()` only** — don't write to the status field directly
- **Config values from `config.py`** — no hardcoded paths, ports, or magic numbers
- **Type hints on every public method** — `mypy backend/` should pass clean
- **No bare `except:`** — catch specific exceptions
- **No `print()`** — use `structlog`
- **No new dependencies without updating `requirements.txt`**

### Workflow

```bash
# 1. Fork and clone
git clone https://github.com/<you>/job-tracker.git

# 2. Branch off main
git checkout -b feat/your-feature-name

# 3. Write tests first if you're touching parsing or status logic
pytest tests/ -v --ignore=tests/e2e   # green before and after

# 4. Lint passes
ruff check backend/ tests/ && mypy backend/

# 5. Open PR against main with a clear description of what changed and why
```

### Good first contributions

- Add portal rules to `portal_rules.yaml` for portals not currently detected
- Improve NLP extraction for edge-case company names (especially multi-word Indian company names)
- Write tests for uncovered parser paths (`pytest --cov=backend --ignore=tests/e2e`)
- Frontend: add keyboard shortcuts to the applications table
- Frontend: mobile-responsive layout (currently desktop-only)
- Extend the AI agent in `src/ai/` to support natural-language queries over your application history

---

## Architecture philosophy

This is intentionally a local-only tool. No backend cloud sync, no user accounts, no analytics phoning home. Your job search data is nobody else's business.

The backend follows strict separation of concerns enforced by convention (see `CLAUDE.md` for the full ruleset). If you're adding a feature and find yourself wanting to bypass one of the DataStore/StatusUpdater/GmailPoller boundaries — don't. Add a method to the appropriate class instead.

The poller runs on a plain `threading.Event` loop (not APScheduler) so it can be cleanly started and stopped inside the FastAPI lifespan, manually triggered via the API, and woken by macOS sleep/wake events — all without external dependencies.

---

## License

MIT. Fork it, hack it, make it track your freelance gigs or apartment applications or whatever — go wild.
