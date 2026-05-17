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
API_HOST = "jobtracker.localhost"
API_PORT = 8000
FRONTEND_PORT = 5173
FRONTEND_PORT_ALT = 5174

# LLM parser (Ollama — open-source local inference)
LLM_ENABLED: bool = os.environ.get("LLM_ENABLED", "true").lower() == "true"
LLM_MODEL: str = os.environ.get("LLM_MODEL", "llama3.2:3b")
LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "http://localhost:11434")
LLM_TIMEOUT_SECONDS: int = int(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))

# Insights
MIN_APPLICATIONS_FOR_INSIGHTS = 10
STALE_DAYS_THRESHOLD = 14
INTERVIEW_RATE_GREEN_THRESHOLD = 0.20  # 20%+ = green
DUPLICATE_FUZZY_THRESHOLD = 85  # rapidfuzz score 0-100
