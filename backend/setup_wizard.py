"""Interactive setup wizard for first-run configuration."""

import sys
from pathlib import Path

import click
import keyring
from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.append(str(Path(__file__).parent.parent))

from backend.config import (
    CREDENTIALS_PATH,
    GMAIL_KEYCHAIN_SERVICE,
    GMAIL_KEYCHAIN_USERNAME,
    GMAIL_SCOPES,
    JOB_TRACKER_DIR,
    LOG_DIR,
)
from backend.db.data_store import DataStore

HOME = Path.home()
PROJECT_ROOT = Path(__file__).parent.parent
PYTHON = sys.executable
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"


def _run_oauth() -> str:
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    return creds.to_json()


def _save_token(token_json: str) -> None:
    keyring.set_password(GMAIL_KEYCHAIN_SERVICE, GMAIL_KEYCHAIN_USERNAME, token_json)


def _install_launchd_plists() -> None:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    api_plist = LAUNCH_AGENTS_DIR / "com.jobtracker.api.plist"
    poller_plist = LAUNCH_AGENTS_DIR / "com.jobtracker.poller.plist"

    api_plist.write_text(f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.jobtracker.api</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON}</string><string>-m</string><string>uvicorn</string>
        <string>backend.main:app</string><string>--host</string><string>localhost</string>
        <string>--port</string><string>8000</string>
    </array>
    <key>WorkingDirectory</key><string>{PROJECT_ROOT}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{LOG_DIR}/api.log</string>
    <key>StandardErrorPath</key><string>{LOG_DIR}/api_error.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONPATH</key><string>{PROJECT_ROOT}</string></dict>
</dict>
</plist>
""")

    poller_plist.write_text(f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.jobtracker.poller</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON}</string><string>backend/poller/scheduler.py</string>
    </array>
    <key>WorkingDirectory</key><string>{PROJECT_ROOT}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{LOG_DIR}/poller.log</string>
    <key>StandardErrorPath</key><string>{LOG_DIR}/poller_error.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONPATH</key><string>{PROJECT_ROOT}</string></dict>
</dict>
</plist>
""")

    import subprocess
    subprocess.run(["launchctl", "load", str(api_plist)], check=False)
    subprocess.run(["launchctl", "load", str(poller_plist)], check=False)


@click.group()
def cli() -> None:
    pass


@cli.command()
def setup() -> None:
    """First-run setup: OAuth, DB init, and launchd installation."""
    click.echo("Job Tracker Setup")
    click.echo("=================")

    if not CREDENTIALS_PATH.exists():
        click.echo(
            f"client_secret.json not found at {CREDENTIALS_PATH}\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials"
            " → your OAuth 2.0 Client ID → Download JSON\n"
            f"Then: mv ~/Downloads/client_secret.json {CREDENTIALS_PATH}"
        )
        sys.exit(1)

    JOB_TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    click.echo("Opening browser for Gmail OAuth…")
    token_json = _run_oauth()
    _save_token(token_json)
    click.echo("Token saved to keychain.")

    click.echo("Initialising database…")
    DataStore()
    click.echo("Database ready.")

    click.echo("Installing launchd services…")
    _install_launchd_plists()

    click.echo(
        "\nSetup complete. Start the tracker with:\n"
        "  launchctl start com.jobtracker.api && launchctl start com.jobtracker.poller"
    )


@cli.command()
def reauth() -> None:
    """Re-authenticate with Gmail OAuth (token refresh)."""
    token_json = _run_oauth()
    _save_token(token_json)
    click.echo("Re-authentication complete.")


if __name__ == "__main__":
    cli()
