import os
from pathlib import Path

# Paths
HOME = Path.home()
JOB_TRACKER_DIR = Path(os.environ.get("JOB_TRACKER_DIR", str(Path(__file__).parent.parent / ".job-tracker")))
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
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN")  # e.g. https://job-tracker-three-green.vercel.app

# Public URL the deployed backend is reachable at — used to build the OAuth redirect_uri
# for the web-based re-auth flow (/poller/reauth/*). Must exactly match a redirect URI
# registered on the OAuth client in Google Cloud Console. Falls back to localhost for
# local development, where the interactive setup_wizard.py flow is used instead.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://{API_HOST}:{API_PORT}")

# Shared secret gating /poller/reauth/start — this endpoint mints a Google OAuth `state`
# that /poller/reauth/callback trusts, so anyone who can call it unauthenticated could bind
# their own Gmail account into this app's keyring. Required on the deployed (non-localhost)
# backend; unset is only safe for local single-user development.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

# LLM parser — Ollama (local) by default, Groq (free-tier hosted) in prod.
# Set LLM_PROVIDER=groq + GROQ_API_KEY to use Groq instead of local Ollama.
LLM_ENABLED: bool = os.environ.get("LLM_ENABLED", "true").lower() == "true"
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "ollama")  # "ollama" | "groq"
LLM_MODEL: str = os.environ.get(
    "LLM_MODEL", "llama3.2:3b" if LLM_PROVIDER == "ollama" else "llama-3.1-8b-instant"
)
LLM_BASE_URL: str = os.environ.get(
    "LLM_BASE_URL",
    "http://localhost:11434" if LLM_PROVIDER == "ollama" else "https://api.groq.com/openai/v1",
)
LLM_API_KEY: str | None = os.environ.get("GROQ_API_KEY")
LLM_TIMEOUT_SECONDS: int = int(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))

# Insights
REEXTRACT_BATCH_LIMIT = 50  # cap per /applications/reextract call to avoid Gmail rate limits/timeouts
MIN_APPLICATIONS_FOR_INSIGHTS = 10
STALE_DAYS_THRESHOLD = 14
INTERVIEW_RATE_GREEN_THRESHOLD = 0.20  # 20%+ = green
DUPLICATE_FUZZY_THRESHOLD = 85  # rapidfuzz score 0-100
