# job-tracker

> *Because copy-pasting job statuses into a spreadsheet at 11 PM is a form of self-harm.*

A fully local Mac app that reads your Gmail, figures out where you are in every job application pipeline, and surfaces the whole thing in a slick dashboard — no SaaS subscriptions, no data leaving your machine, no VC-funded "free tier" with a 100-application limit.

---

## What it actually does

- **Polls Gmail every 5 minutes** (read-only OAuth, never touches anything) and looks for job-related emails
- **Parses them with NLP** (spaCy + YAML rules) to extract company names, portals (Greenhouse, Workday, Lever, etc.), and status signals
- **Advances application status automatically** through a defined pipeline: `Applied → In Review → Phone Screen → Interview → Offer → Rejected`
- **Detects duplicates** so one application doesn't show up twelve times because Workday sends a new email every time someone breathes
- **Surfaces analytics**: Sankey flow diagram, funnel donut, KPI cards, conversion rates
- **Icon-driven UI** — every action button replaced with a Lucide icon (with proper `aria-label` for accessibility), search has an inline icon, pagination uses chevrons, sort columns show directional arrows
- **Dark mode** because your eyes matter
- **Sleep/wake aware** — pauses polling when your Mac is asleep, resumes when it wakes up
- Runs as **launchd daemons** so it's always on in the background

---

## Tech stack

### Backend
| Thing | What it does |
|---|---|
| **Python 3.11** | Core runtime |
| **FastAPI** | REST API on `jobtracker.localhost:8000` |
| **SQLModel + SQLite** | ORM + local DB at `~/.job-tracker/applications.db` |
| **spaCy** (`en_core_web_sm`) | NLP for company/role extraction from email text |
| **APScheduler** | Runs the Gmail polling loop every 5 minutes |
| **google-api-python-client** | Gmail API (read-only) |
| **structlog** | Structured JSON logging |
| **tenacity** | Retry logic with exponential backoff |
| **rapidfuzz** | Fuzzy deduplication of company names |
| **pyobjc-framework-Cocoa** | Hooks into macOS sleep/wake notifications |

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
│   ├── db/
│   │   ├── models.py          # SQLModel table definitions (schema source of truth)
│   │   └── data_store.py      # All DB access — nothing talks to SQLite directly
│   ├── engine/
│   │   ├── status_updater.py  # Status transitions (only place allowed to advance status)
│   │   ├── insights_engine.py # Analytics computations
│   │   └── duplicate_detector.py
│   ├── parser/
│   │   ├── email_parser.py    # NLP-based email parsing
│   │   ├── status_signals.py  # Pattern matching for status extraction
│   │   └── portal_rules.yaml  # Edit this to add new job portals — no code needed
│   ├── poller/
│   │   ├── gmail_poller.py    # Gmail API polling
│   │   ├── scheduler.py       # APScheduler setup
│   │   └── sleep_watcher.py   # macOS sleep/wake integration
│   └── setup_wizard.py        # One-time OAuth + launchd setup
├── frontend/src/
│   ├── components/            # ApplicationsTable, AnalyticsPanel, Filters, etc.
│   └── contexts/              # ThemeContext (dark mode)
├── tests/
│   ├── unit/                  # Parser, engine, poller unit tests
│   ├── integration/           # API route tests
│   └── e2e/                   # Playwright end-to-end tests
├── start.sh                   # Quick launcher for both services
└── portal_rules.yaml          # User-editable portal detection rules
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
   mv ~/Downloads/client_secret_*.json ~/Codes/job-tracker/.job-tracker/client_secret.json
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

### Useful commands

```bash
# Stop/start the background daemons
launchctl stop com.jobtracker.api
launchctl stop com.jobtracker.poller
launchctl start com.jobtracker.api
launchctl start com.jobtracker.poller

# Re-authenticate if your OAuth token expires
python backend/setup_wizard.py reauth

# Watch the logs
tail -f ~/.job-tracker/logs/poller.log
tail -f ~/.job-tracker/logs/api_error.log

# Run the backend test suite
pytest tests/ -v

# Run the frontend test suite
cd frontend && npm test

# Lint and format
ruff check backend/ tests/
ruff format backend/ tests/

# Type check
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

Add your portal, restart the poller, done.

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
pytest tests/ -v   # should be green before and after

# 4. Lint passes
ruff check backend/ tests/ && mypy backend/

# 5. Open PR against main with a clear description of what changed and why
```

### Good first contributions

- Add portal rules to `portal_rules.yaml` for portals not currently detected
- Improve NLP extraction for edge-case company names
- Write tests for uncovered parser paths (check coverage with `pytest --cov=backend`)
- Frontend: add keyboard shortcuts to the applications table
- Frontend: mobile-responsive layout (it's currently desktop-only)

---

## Architecture philosophy

This is intentionally a local-only tool. No backend cloud sync, no user accounts, no analytics phoning home. Your job search data is nobody else's business.

The backend follows strict separation of concerns enforced by convention (see `CLAUDE.md` for the full rules). If you're adding a feature and find yourself wanting to bypass one of the DataStore/StatusUpdater/GmailPoller boundaries — don't. Add a method to the appropriate class instead.

---

## License

MIT. Fork it, hack it, make it track your freelance gigs or apartment applications or whatever — go wild.
