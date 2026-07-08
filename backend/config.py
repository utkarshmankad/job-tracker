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
MIN_APPLICATIONS_FOR_INSIGHTS = 10
STALE_DAYS_THRESHOLD = 14
INTERVIEW_RATE_GREEN_THRESHOLD = 0.20  # 20%+ = green
DUPLICATE_FUZZY_THRESHOLD = 85  # rapidfuzz score 0-100
