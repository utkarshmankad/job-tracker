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

# API
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_KEYCHAIN_SERVICE = "job-tracker-gmail"
GMAIL_KEYCHAIN_USERNAME = "oauth-token"

# Poller
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

# Cache — speeds up repeated reads (e.g. re-fetching /applications or /insights on every
# tab switch) by caching short-lived GET responses in Redis. Fails open: if Redis is
# unreachable, every request just falls through to the DB as if caching were off.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "true").lower() == "true"
CACHE_CONNECT_TIMEOUT_SECONDS = 0.2  # fail fast when Redis isn't running (e.g. local dev, CI)
APPLICATIONS_CACHE_TTL_SECONDS = int(os.environ.get("APPLICATIONS_CACHE_TTL_SECONDS", "20"))
INSIGHTS_CACHE_TTL_SECONDS = int(os.environ.get("INSIGHTS_CACHE_TTL_SECONDS", "20"))
