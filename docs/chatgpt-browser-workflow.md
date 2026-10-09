# ChatGPT signed-in browser collection

## Decision

Job-site acquisition is performed by a **local ChatGPT scheduled task using the ChatGPT
browser extension and the user's existing Brave session**. The task reads LinkedIn and
Indeed's own Applied pages and submits the minimal rows through Job Tracker's authenticated
Browser Agent Import panel.

The separate Playwright collector remains available only as a compatibility and diagnostic
path. Do not extend it, schedule it, add another browser profile, or build a custom extension.

## Deviation from the intended workflow

| Intended | Phase 3 implementation | Decision |
|---|---|---|
| Use the browser session already signed into job sites | Dedicated Playwright profile | Replace |
| Schedule with ChatGPT | macOS launchd template | Replace |
| Read LinkedIn and Indeed visually | Site-specific Playwright adapters | Stop extending |
| Send observations to the tracker | Scoped ingestion, provenance and resolver | Keep |
| Avoid duplicate applications | Stable IDs, evidence resolution and idempotency | Keep |
| Power dashboard and review | Sources metrics and review queue | Keep |

## Runtime workflow

1. A local ChatGPT scheduled task runs while the Mac, ChatGPT desktop app and Brave are
   available.
2. It uses `@Brave`, never the cloud browser. Cloud browser cookies are separate from the
   local Brave profile.
3. It visits only the LinkedIn and Indeed application-history pages.
4. It stops on sign-out, CAPTCHA, challenge, consent, rate limiting, incomplete history or
   uncertainty. It never applies, withdraws, messages or edits a profile.
5. It extracts only: stable job ID, company, role, applied date, displayed status and
   canonical job URL.
6. It opens the production Job Tracker in the same signed-in browser, selects **Sources →
   Browser Agent Import**, pastes one source at a time, and selects **Preview**.
7. It compares the preview count to the source count, then selects **Import**.
8. The backend validates the source host and stable ID, creates a provenance run, and uses
   the existing Phase 3 resolver. Repeated rows are unchanged; nothing auto-merges.

## Import JSON

Paste an array, one source at a time:

```json
[
  {
    "source_item_id": "stable-site-job-id",
    "company": "Company shown on the applied card",
    "role": "Role shown on the applied card",
    "applied_on": "2026-10-10",
    "status": "applied",
    "raw_status": "Applied",
    "job_url": "https://in.indeed.com/viewjob?jk=stable-site-job-id"
  }
]
```

`source_item_id`, `company`, `role`, and a matching LinkedIn/Indeed HTTPS URL are mandatory.
The server rejects unknown fields, duplicate IDs, other hosts, oversized batches and missing
CSRF/session authentication.

## Scheduling prerequisites

- ChatGPT desktop app is running on the Mac.
- ChatGPT's browser extension is connected to Brave.
- LinkedIn, Indeed and Job Tracker are signed in in Brave.
- Browser permissions for only those three sites have been reviewed.
- A supervised run has successfully previewed and imported both sources.
- The task stays quiet when nothing changes and reports sign-out, challenge, count mismatch,
  review items, failed rows or new applications.

Do not activate the schedule until the production Browser Agent Import panel is deployed and
one supervised end-to-end run passes.
