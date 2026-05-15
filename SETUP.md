# Job Tracker — Setup Guide

## Prerequisites
- Python 3.11+
- macOS 13+
- Gmail account used for job applications
- Google Cloud project with Gmail API enabled (see cost-and-setup.md)

## Install

```bash
cd ~/job-tracker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

## Google Cloud Setup (one-time, ~15 minutes)
1. Create project at console.cloud.google.com
2. Enable Gmail API: APIs & Services → Library → Gmail API → Enable
3. OAuth consent screen: External → add your Gmail as test user → scope: gmail.readonly → Publish App (to avoid 7-day token expiry)
4. Credentials: Create → OAuth 2.0 Client ID → Desktop app → Download JSON
5. `mv ~/Downloads/client_secret.json ~/Codes/job-tracker/.job-tracker/client_secret.json`

## First Run

```bash
source .venv/bin/activate
python backend/setup_wizard.py setup
```

This will:
- Open browser for Gmail OAuth (click through "unverified app" warning)
- Create database at ~/Codes/job-tracker/.job-tracker/applications.db
- Install and start background services via launchd

## Start / Stop Services

```bash
launchctl start com.jobtracker.api
launchctl start com.jobtracker.poller
launchctl stop com.jobtracker.api
launchctl stop com.jobtracker.poller
```

## Dashboard
Open http://localhost:5173 in your browser (after starting frontend).

## Re-authenticate (if token issues)
```bash
python backend/setup_wizard.py reauth
```

## Logs
```bash
tail -f ~/Codes/job-tracker/.job-tracker/logs/poller.log
tail -f ~/Codes/job-tracker/.job-tracker/logs/api_error.log
```
