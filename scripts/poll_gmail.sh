#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# poll_gmail.sh — one-shot Gmail poll for job-tracker
#
# Usage:
#   ./scripts/poll_gmail.sh              # poll and update the database
#   ./scripts/poll_gmail.sh --dry-run    # check credentials only
#
# To run daily via launchd (macOS), install the plist in:
#   ~/Library/LaunchAgents/com.jobtracker.poll.plist
# (a template is printed when you pass --install-launchd)
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${REPO_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "ERROR: virtual-env Python not found at ${VENV_PYTHON}"
    echo "       Create it with:  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

if [[ "${1:-}" == "--install-launchd" ]]; then
    PLIST_PATH="${HOME}/Library/LaunchAgents/com.jobtracker.poll.plist"
    cat > "${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jobtracker.poll</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REPO_DIR}/scripts/poll_gmail.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${HOME}/Library/Logs/jobtracker-poll.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/Library/Logs/jobtracker-poll.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST
    launchctl load "${PLIST_PATH}"
    echo "Installed and loaded launchd job — will run daily at 08:00."
    echo "Plist: ${PLIST_PATH}"
    echo "Logs:  ${HOME}/Library/Logs/jobtracker-poll.log"
    exit 0
fi

cd "${REPO_DIR}"
exec "${VENV_PYTHON}" -m backend.poller.poll_once_cli "$@"
