# Collector operations (local)

How to set up, run, verify and schedule the read-only collector on your own Mac. The
design is in [`phase-3-source-collection.md`](phase-3-source-collection.md).

## 1. Install (once)

```bash
cd ~/Codes/job-tracker            # a checkout of this repository
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-collector.txt
```

The collector drives your installed Google Chrome (`channel = "chrome"`). To use
Playwright's bundled Chromium instead, set `channel = ""` and run
`python -m playwright install chromium`.

## 2. Configure

Create `~/Library/Application Support/JobTrackerCollector/config.toml`. Override the
location with `JOB_TRACKER_COLLECTOR_HOME`.

```toml
api_url = "https://job-tracker-api-verdant-haze-8797.fly.dev"

[browser]
# A dedicated profile used only for collection (recommended). It is referenced by path,
# never copied. Your everyday Chrome profile is refused unless you set
# allow_primary_profile = true — and then Chrome must be closed while collecting.
user_data_dir = "~/Library/Application Support/JobTrackerCollector/browser-profile"
channel = "chrome"

[limits]
max_pages = 10            # 1–50
min_delay_seconds = 4     # >= 2; human-paced
max_delay_seconds = 9

[diagnostics]
enabled = false           # encrypted page snapshots for repairing an adapter; off by default
ttl_hours = 24            # <= 72

[[sources]]
source_key = "linkedin"
account_label = "default"

[[sources]]
source_key = "naukri"
```

Check it: `python scripts/collect.py validate-config`.

## 3. Sign in and enroll

1. **Sign in to each site yourself.** Run `python scripts/collect.py open-profile`. A
   visible browser opens on the dedicated profile; sign in normally, completing any MFA
   or CAPTCHA, then press Enter. The collector never sees or stores those credentials.
2. **Create a collector.** In Job Tracker go to **Sources → Set up a collector**, choose
   its sources and click **Create setup command**. Copy the command. It is valid once,
   for 10 minutes.
3. **Enroll.** Run the command on this Mac:
   ```bash
   python scripts/collect.py enroll --api-url https://… --code …
   ```
   The credential goes straight into the macOS Keychain (service `job-tracker-collector`).
   It is never printed or written to disk.

## 4. Commands

| Command | What it does |
|---|---|
| `validate-config` | checks config, profile path, sources, adapter support and keychain credential |
| `adapters` | lists adapters, pagination style and live-verification status |
| `check-session --source S` | opens the history page and reports the session state only (signed in / signed out / challenge / …) |
| `run --source S --dry-run` | collects and validates, sends nothing, saves the validated observations to `state/pending/*.json` for you to inspect |
| `submit <pending file>` | sends a dry run's saved observations as one run (after you reviewed them) |
| `run --source S` | collects one source and sends it |
| `run-all [--dry-run]` | every enabled source, one after another, in one browser session |
| `diagnostics [--local-only]` | content-free local run log, plus this collector's server metrics |
| `clear-diagnostics [--yes]` | deletes local run logs, dry-run files, encrypted snapshots and their key |

**Exit codes.** A command exits non-zero if any source did not finish `succeeded`.
`run`/`run-all` hold a lock (`state/collector.lock`), so two runs never overlap.

## 5. Verifying an adapter (required before automatic creation)

No adapter is live-verified yet. Until one is, its observations are `unverified`: they
link only on strong identifiers, and every new item goes to review. To verify one, with
you watching:

1. **Check the session.** `check-session --source linkedin` must report `authenticated`.
2. **Collect without sending.** Run `run --source linkedin --dry-run`, then open the
   saved JSON. Compare each item with the site: company, role, date, status label and
   item ID. Check the count against what the site shows, including all pages.
3. **If it drifted.** If the run ended `selector_drift` or `unexpected_page`, enable
   diagnostics, run again, and fix the selectors in `collector/adapters/sites.py` using
   the decrypted snapshot. Snapshots are decryptable only with the keychain key. Then
   run `clear-diagnostics`.
4. **Refresh the fixtures.** Update `tests/fixtures/collector/<source>/` with a
   **sanitized, synthetic** page of the same structure. Never commit a real page.
5. **Record it.** Set `LIVE_VERIFIED = "YYYY-MM-DD"` on the adapter, run the tests, and
   commit.
6. **Send for real.** Run without `--dry-run`, then check **Sources** in Job Tracker.

## 6. Scheduling on macOS (optional; not enabled automatically)

`launchd/com.jobtracker.collector.plist.template` runs `run-all` once a day at 10:30.

```bash
mkdir -p ~/Library/Logs/JobTrackerCollector
sed -e "s#{PYTHON}#$PWD/.venv/bin/python#" -e "s#{PROJECT_ROOT}#$PWD#" -e "s#{HOME}#$HOME#" \
    launchd/com.jobtracker.collector.plist.template > ~/Library/LaunchAgents/com.jobtracker.collector.plist
launchctl load ~/Library/LaunchAgents/com.jobtracker.collector.plist      # enable
launchctl unload ~/Library/LaunchAgents/com.jobtracker.collector.plist    # disable
```

How scheduled runs behave:
- They happen in your user session, because your signed-in profile is there, and they
  open a visible browser window.
- If a site signs you out or shows a challenge, the run stops and **Sources** shows
  "Needs your attention". Resolve it with `open-profile`.
- Keep the schedule at most daily. Collection should stay low-frequency.

## 7. When something goes wrong

| Code (Sources page / CLI) | Meaning | What to do |
|---|---|---|
| `signed_out` | the profile is not signed in to the site | `open-profile`, sign in, run again |
| `challenge` | CAPTCHA / verification / unusual-activity page | complete it yourself in `open-profile`; wait before retrying |
| `rate_limited` | the site is limiting requests | wait (hours), keep the schedule daily |
| `consent_required` | a consent/terms wall | resolve it in the browser |
| `selector_drift` | the page layout changed | §5; the adapter needs maintenance |
| `unexpected_page` | redirected somewhere unknown (or another host) | `check-session`; open the page yourself |
| `page_limit` | more history than `max_pages` | normal for a first run; raise `max_pages` once if needed |
| `invalid_items` | some rows failed validation and were not sent | check with `--dry-run` |
| `submission_failed` | the tracker could not be reached or refused the batch | check `validate-config`, network, credential |
| `unauthorized` (CLI) | credential rotated or revoked | create a new setup command and `enroll` again |

**Revoking a collector:** use **Sources → Collectors → Revoke**. It stops working
immediately. Remove the local copy with `security delete-generic-password -s
job-tracker-collector` (or Keychain Access).
