import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Paths
HOME = Path.home()
JOB_TRACKER_DIR = Path(
    os.environ.get("JOB_TRACKER_DIR", str(Path(__file__).parent.parent / ".job-tracker"))
)
DB_PATH = JOB_TRACKER_DIR / "applications.db"
CREDENTIALS_PATH = JOB_TRACKER_DIR / "client_secret.json"
LOG_DIR = JOB_TRACKER_DIR / "logs"
PORTAL_RULES_PATH = Path(__file__).parent / "parser" / "portal_rules.yaml"

# Database recovery and migrations — see docs/database-operations.md.
# Backups live beside the database (on the Fly volume in production), one directory each.
BACKUP_DIR_NAME = "backups"
BACKUP_DIR = JOB_TRACKER_DIR / BACKUP_DIR_NAME
# Default number of verified backups kept when a backup run is asked to prune (--keep).
BACKUP_RETENTION_COUNT = 14
# While this file exists the API starts in maintenance mode: no DataStore, no poller, every
# data endpoint returns 503. Used to quiesce writers before a migration or restore.
MAINTENANCE_FLAG_PATH = JOB_TRACKER_DIR / "MAINTENANCE"

# API
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_KEYCHAIN_SERVICE = "job-tracker-gmail"
GMAIL_KEYCHAIN_USERNAME = "oauth-token"
# Environment variable carrying the Gmail authorized-user token JSON (used on Fly instead of
# the keychain). When set, client_secret.json is only needed for the web re-auth flow.
GMAIL_TOKEN_ENV_VAR = "GMAIL_TOKEN_JSON"

# Poller
# POLLER_ENABLED=false starts the API without the Gmail poller: no Gmail credentials are
# loaded, the keychain is never read, no Google API client is built, and neither the poller
# thread nor sleep/wake monitoring starts. The test suite always runs this way
# (tests/conftest.py). Default true keeps local development and production unchanged.
POLLER_ENABLED: bool = os.environ.get("POLLER_ENABLED", "true").lower() == "true"
POLL_INTERVAL_SECONDS = 300  # 5 minutes
BACKFILL_DAYS = 180  # 6 months on first run

# Dashboard
API_HOST = os.environ.get("API_HOST", "jobtracker.localhost")
API_PORT = int(os.environ.get("API_PORT", "8000"))
FRONTEND_PORT = 5173
FRONTEND_PORT_ALT = 5174
FRONTEND_ORIGIN = os.environ.get(
    "FRONTEND_ORIGIN"
)  # e.g. https://job-tracker-three-green.vercel.app

# Public URL the API is reached at by the browser — used to build the OAuth redirect_uri
# for the web-based Gmail re-auth flow (/poller/reauth/*). Must exactly match a redirect URI
# registered on the OAuth client in Google Cloud Console. In production this is the Vercel
# origin (which proxies /api/* to Fly) so the session cookie accompanies the callback.
# Falls back to localhost for local development.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://{API_HOST}:{API_PORT}")

# Deployment environment. "production" makes authentication fail closed: the API refuses to
# start unless every required AUTH_* value is configured. The Docker image sets it.
APP_ENV: str = os.environ.get("APP_ENV", "development").strip().lower()

# Authentication — a single human owner. See docs/authentication.md.
#   AUTH_MODE=google (default): the browser obtains a Google ID token via Google Identity
#     Services; the backend verifies it and only accepts AUTH_ALLOWED_EMAIL.
#   AUTH_MODE=local: explicit loopback-only developer sign-in; refused when APP_ENV=production.
# There is no "auth disabled" mode.
AUTH_MODE: str = os.environ.get("AUTH_MODE", "google").strip().lower()
AUTH_ALLOWED_EMAIL: str | None = os.environ.get("AUTH_ALLOWED_EMAIL", "").strip().lower() or None
# Web-application OAuth client ID used for Google Sign-In (public, not a secret).
AUTH_GOOGLE_CLIENT_ID: str | None = os.environ.get("AUTH_GOOGLE_CLIENT_ID", "").strip() or None
# HMAC key for session cookies, CSRF tokens and login nonces. Rotating it signs everyone out.
AUTH_SESSION_SECRET: str | None = os.environ.get("AUTH_SESSION_SECRET") or None
AUTH_SESSION_SECRET_MIN_LENGTH = 32
AUTH_SESSION_TTL_SECONDS: int = int(os.environ.get("AUTH_SESSION_TTL_SECONDS", "43200"))  # 12 h
AUTH_SESSION_TTL_MIN_SECONDS = 300
AUTH_SESSION_TTL_MAX_SECONDS = 7 * 24 * 3600
AUTH_LOGIN_NONCE_TTL_SECONDS = 600
GOOGLE_ID_TOKEN_CLOCK_SKEW_SECONDS = 10
# Gmail re-auth `state` minted by /poller/reauth/start is only honoured this long.
REAUTH_STATE_TTL_SECONDS = 600

# Schema migrations at startup. Outside production an outdated database is backed up and
# upgraded automatically unless DB_AUTO_MIGRATE=false. In production this flag is ignored:
# an outdated schema puts the API in maintenance mode until an operator runs
# scripts/migrate_database.py (see docs/database-operations.md).
DB_AUTO_MIGRATE: bool = os.environ.get("DB_AUTO_MIGRATE", "true").lower() == "true"

# Rate limits (in-process, per client address): sign-in attempts, and sensitive operations
# (deletes, merges, bulk edits, imports, exports, poller and Gmail re-auth controls,
# diagnostics).
AUTH_RATE_LIMIT_ATTEMPTS = 10
AUTH_RATE_LIMIT_WINDOW_SECONDS = 300
SENSITIVE_RATE_LIMIT_REQUESTS = 30
SENSITIVE_RATE_LIMIT_WINDOW_SECONDS = 60

# LLM parser — Ollama (local) by default, Groq (free-tier hosted) in prod.
# Set LLM_PROVIDER=groq + GROQ_API_KEY to use Groq instead of local Ollama.
LLM_ENABLED: bool = os.environ.get("LLM_ENABLED", "true").lower() == "true"
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "ollama")  # "ollama" | "groq"
LLM_MODEL: str = os.environ.get(
    # llama-3.1-8b-instant retired by Groq on 2026-08-16; migrated default to gpt-oss-20b.
    "LLM_MODEL",
    "llama3.2:3b" if LLM_PROVIDER == "ollama" else "openai/gpt-oss-20b",
)
LLM_BASE_URL: str = os.environ.get(
    "LLM_BASE_URL",
    "http://localhost:11434" if LLM_PROVIDER == "ollama" else "https://api.groq.com/openai/v1",
)
LLM_API_KEY: str | None = os.environ.get("GROQ_API_KEY")
LLM_TIMEOUT_SECONDS: int = int(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))

# Insights
# cap per /applications/reextract call to avoid Gmail rate limits/timeouts
REEXTRACT_BATCH_LIMIT = 50
MIN_APPLICATIONS_FOR_INSIGHTS = 10
STALE_DAYS_THRESHOLD = 14
INTERVIEW_RATE_GREEN_THRESHOLD = 0.20  # 20%+ = green
DUPLICATE_FUZZY_THRESHOLD = 85  # rapidfuzz score 0-100

# Evidence (Phase 2). Gmail's preview snippet is stored, capped; message bodies never are.
EVIDENCE_SNIPPET_MAX_CHARS = 500
# Identity matching considers applications from this many days back.
IDENTITY_LOOKBACK_DAYS = 180

# Identity resolver (backend/engine/identity_resolver.py; docs/phase-2-identity-resolution.md
# §11). Deterministic weighted scoring: strong identifiers outweigh text similarity.
RESOLVER_WEIGHTS: dict[str, int] = {
    # strong identifiers
    "same_gmail_thread": 100,
    "same_external_job_id": 100,  # within the same source
    "same_canonical_job_url": 90,
    "sender_linked_by_human": 60,
    "sender_linked_by_resolver": 40,
    # supporting signals
    "same_external_job_id_other_source": 25,
    "known_company_domain": 25,
    "company_exact": 35,
    "company_similar": 15,
    "role_exact": 35,
    "role_similar": 20,
    "sole_active_application_at_company": 30,
    "date_in_window": 10,
    "source_match": 5,
    # negative signals
    "external_job_id_conflict": -100,
    "company_conflict": -60,
    "role_conflict": -45,
    "date_out_of_window": -40,
    "terminal_application_new_acknowledgement": -30,
    "job_url_differs": -20,
    "text_mismatch_in_thread": -10,  # thread wins over extraction noise
}
RESOLVER_AUTO_LINK_SCORE = 80  # best score needed to link without review
RESOLVER_AUTO_LINK_MARGIN = 25  # and its lead over the runner-up
RESOLVER_REVIEW_SCORE = 40  # a candidate at/above this blocks "new application"
RESOLVER_MAX_CANDIDATES = 25
RESOLVER_DATE_WINDOW_BEFORE_DAYS = 14  # evidence this long before applied_date still fits
RESOLVER_DATE_WINDOW_AFTER_DAYS = 180  # …and this long after
RESOLVER_COMPANY_SIMILARITY = 92  # rapidfuzz ratio for "company_similar"
RESOLVER_COMPANY_CONFLICT_BELOW = 80
RESOLVER_ROLE_SIMILARITY = 90  # rapidfuzz token_set_ratio for "role_similar"
RESOLVER_ROLE_CONFLICT_BELOW = 70
RESOLVER_PROCESSING_CLAIM_TTL_SECONDS = 600  # a crashed worker's claim expires after this
DUPLICATE_SUGGESTION_SCORE = 70  # pairwise score for "possible duplicate" suggestions
MERGE_MAX_APPLICATIONS = 20  # largest group one merge may combine

# Cache — speeds up repeated reads (e.g. re-fetching /applications or /insights on every
# tab switch) by caching short-lived GET responses in Redis. Fails open: if Redis is
# unreachable, every request just falls through to the DB as if caching were off.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "true").lower() == "true"
CACHE_CONNECT_TIMEOUT_SECONDS = 0.2  # fail fast when Redis isn't running (e.g. local dev, CI)
APPLICATIONS_CACHE_TTL_SECONDS = int(os.environ.get("APPLICATIONS_CACHE_TTL_SECONDS", "20"))
INSIGHTS_CACHE_TTL_SECONDS = int(os.environ.get("INSIGHTS_CACHE_TTL_SECONDS", "20"))

# --- Phase 3 source collection (docs/phase-3-source-collection.md) ---------------------
# Sources a collector may be scoped to. Employer portals use "employer-<slug>" keys and are
# only collected by an explicitly configured adapter, never heuristically.
COLLECTOR_SOURCES = ("linkedin", "naukri", "indeed", "instahyre", "careernet")
COLLECTOR_EMPLOYER_SOURCE_PATTERN = r"^employer-[a-z0-9][a-z0-9-]{1,38}$"
COLLECTOR_CONTRACT_VERSION = 1
COLLECTOR_ENROLLMENT_TTL_SECONDS = 600  # a setup code is single-use and expires quickly
COLLECTOR_MAX_BATCH_OBSERVATIONS = 100
COLLECTOR_MAX_BODY_BYTES = 256 * 1024
COLLECTOR_BATCH_MAX_AGE_SECONDS = 900  # replay window: older unseen batches are refused
COLLECTOR_RATE_LIMIT_REQUESTS = 120  # per collector
COLLECTOR_RATE_LIMIT_WINDOW_SECONDS = 60
COLLECTOR_ENROLL_RATE_LIMIT_ATTEMPTS = 10  # per client address
COLLECTOR_ENROLL_RATE_LIMIT_WINDOW_SECONDS = 300
COLLECTOR_MAX_OBSERVATION_AGE_DAYS = 400

# Reserved internal collector used by the authenticated Browser Agent Import surface.
# It never receives a bearer secret: ChatGPT operates the already signed-in Job Tracker UI,
# while the backend records those imports through the same idempotent Phase 3 pipeline.
BROWSER_WORKFLOW_COLLECTOR_TOKEN_ID = "chatgpt-browser-workflow"
BROWSER_WORKFLOW_COLLECTOR_NAME = "ChatGPT browser workflow"
BROWSER_WORKFLOW_VERSION = "chatgpt-browser-v1"
BROWSER_WORKFLOW_SOURCES = ("linkedin", "indeed")
BROWSER_WORKFLOW_ALLOWED_HOSTS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com", "www.linkedin.com"),
    "indeed": ("indeed.com", "www.indeed.com", "in.indeed.com"),
}
