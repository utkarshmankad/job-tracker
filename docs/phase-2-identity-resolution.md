# Phase 2 — Evidence model and identity resolution

Status: implemented on `feat/phase-2-identity-resolution` (base `origin/main` @ `a579969`).
Builds on Phase 1 (auth, Alembic, verified backups). Plan context:
[`phase-1-implementation.md`](phase-1-implementation.md) §4 Steps 4–5.

## Goal

Every observation the tracker receives — a Gmail message, a portal import, a future
browser-agent observation, a manual note — is stored once as **evidence** and linked to an
application when its identity is clear. A message that is not an application acknowledgement
(scheduling, interview follow-up, rejection, any later-stage mail) never creates an
application just because its Gmail message ID is new. Unclear cases stay visible for review.

## 1. Data model (additive)

### `evidence` (new table)

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `evidence_type` | varchar | `email` \| `portal_import` \| `browser_observation` \| `manual` |
| `source` | varchar | `gmail` \| `linkedin` \| `naukri` \| `indeed` \| `instahyre` \| `careernet` \| `company_portal` \| `other` (the *channel*; for Gmail the matched job portal is in metadata) |
| `external_id` | varchar, null | Gmail message ID, portal record ID, … |
| `thread_id` | varchar, null, indexed | Gmail thread ID |
| `sender`, `recipient` | varchar, null | `recipient` is not populated for Gmail (it is the owner's own address) |
| `subject`, `normalized_subject` | varchar, null | normalized: `Re:`/`Fwd:` prefixes stripped, case-folded, whitespace collapsed |
| `snippet` | varchar(≤500), null | Gmail's own preview snippet only — never the message body |
| `occurred_at` | datetime (UTC), indexed | when it happened (email `Date`) |
| `captured_at` | datetime (UTC) | when the tracker recorded it |
| `raw_metadata` | JSON | structured, non-content metadata: parser classification, portal, extracted company/role/job URL, status signal, backfill provenance |
| `content_fingerprint` | varchar(64), **unique** | SHA-256, see §3 |
| `processing_status` | varchar, indexed | `pending` \| `linked` \| `created_application` \| `needs_review` \| `informational` \| `ignored` \| `error` |
| `review_reason` | varchar, null | why it needs review (`ambiguous_company`, `follow_up_without_application`, `manually_unlinked`, …) |
| `application_id` | FK → `application.id`, null, indexed | |
| `link_method` | varchar, null | `thread` \| `job_url` \| `external_job_id` \| `company_role` \| `company_only` \| `created` \| `manual` \| `backfill` |
| `link_confidence` | float, null | 0–1 |
| `created_at`, `updated_at` | datetime (UTC) | |

Enumerated columns are plain `VARCHAR` validated in Python (`StrEnum`), not `SAEnum`: a value
added by a later release must not make older code raise `LookupError` when reading the row
(the failure class the Phase 1 enum diagnostic exists for).

### `application` (new nullable columns only)

| Column | Why |
|---|---|
| `normalized_company` (indexed) | identity matching without re-normalizing every row per message |
| `normalized_role` | company+role matching |
| `canonical_job_url` (indexed) | exact job-URL identity (tracking parameters stripped) |
| `external_job_id` (indexed) | portal job IDs (portal imports; Gmail does not extract them yet) |
| `last_evidence_at` | newest linked evidence; recomputable from `evidence` |

No column is removed or renamed. `thread_ids` JSON, `applicationthreadid`, `processedmessage`
and `prospect` keep working exactly as before. `source_account` was considered and **not**
added: there is one owner and one Gmail account, so it would carry no information yet.

The three derived identity columns are computed by `DataStore` from `company`, `role` and
`job_url` whenever an application is saved, and filled in for rows where they are NULL by
idempotent runtime maintenance (the same pattern as the Phase 1 thread-index backfill). That
keeps the normalization rules in one place (`backend/engine/normalization.py`) and repairs rows
written by an older release.

## 2. Data flow (Gmail)

```
Gmail list/history ──► message id ──► processedmessage? ──yes──► skip (unchanged ledger)
                                         │ no
                                         ▼
                     fetch metadata (From/Subject/Date) ──► INSERT evidence (idempotent,
                                         │                  minimal: ids, thread, date)
                                         ▼
                                   EmailParser.parse
             ┌───────────────────────────┼──────────────────────────────┐
             ▼                           ▼                              ▼
     not job mail / suppressed     LinkedIn prospect            job-related (ParsedApplication)
     status=ignored, no sender/    upsert prospect; evidence    add sender/subject/snippet +
     subject/snippet stored        details; status=informational parser metadata; classify kind
                                                                        │
                                                         IdentityResolver.resolve
                         ┌───────────────────┬──────────────────────────┼────────────────────┐
                         ▼                   ▼                          ▼                    ▼
                     match found        ambiguous                 no match, kind=        no match, kind=
                     link + merge       needs_review              acknowledgement        follow_up/status
                     thread + status    (reason)                  create app + link      needs_review
                     transition                                    (created)              (reason)
                                         └──────── mark processedmessage (unchanged ledger) ───────┘
```

* The processed-message ledger stays the first idempotency guard; evidence uniqueness is the
  second. Re-running a message (crash between steps, `backfill_portal` clearing a marker)
  finds the existing evidence row and re-classifies it instead of inserting a copy.
* Status changes still go only through `StatusUpdater._advance_status()`.
* Message bodies fetched for company/role refinement are used in memory only, as before.

## 3. Identity and idempotency rules

**Evidence fingerprint** (`backend/engine/normalization.evidence_fingerprint`, SHA-256 hex,
versioned `evidence-v1`, fields joined with U+001F so no field can bleed into another):

* with an external ID: `evidence_type`, `source`, `external_id` — so the same Gmail message
  always yields the same fingerprint regardless of later detail updates;
* without: `evidence_type`, `source`, `thread_id`, normalized sender address, normalized
  recipient, `normalized_subject`, `occurred_at` truncated to the second in UTC, and the
  whitespace-collapsed snippet.

Never Python's `hash()` (randomized per process). Uniqueness is enforced by the database:
unique `content_fingerprint` and unique `(source, external_id)` (SQLite treats NULLs as
distinct, so rows without an external ID are governed by the fingerprint). Inserts use
`INSERT … ON CONFLICT DO NOTHING` followed by a read, so concurrent writers cannot create two
rows; the loser simply receives the winner's row.

**Message kind** (`classify_message_kind`):

1. a parsed status signal → `status_update`;
2. subject starts with `Re:`/`Fwd:` or mentions follow-up vocabulary (interview, schedule,
   availability, assessment, reminder, next steps, feedback, update on your application, …)
   → `follow_up`;
3. otherwise → `acknowledgement`.

**Application resolution** (`IdentityResolver`, first rule that decides wins):

| # | Rule | Link method | Confidence |
|---|---|---|---|
| 1 | Gmail thread already mapped to an application | `thread` | 1.0 |
| 2 | Canonical job URL equals exactly one application's | `job_url` | 0.95 (more than one → ambiguous) |
| 3 | Status-bearing mail and exactly one application at that normalized company (180-day window) | `company_only` | 0.6 |
| 4 | Fuzzy `company role` ≥ `DUPLICATE_FUZZY_THRESHOLD`, single best candidate | `company_role` | score / 100 (tie between different applications → ambiguous) |
| 5 | Status-bearing mail, several applications at that company, none decided above | — | ambiguous `ambiguous_company` |

Rules 2–4 keep the Phase 1 `DuplicateDetector.find_duplicate` priority so existing matching
behaviour does not drift; rule 5 and the tie checks are new. Outcomes:

* match → link evidence, merge thread, apply status transition;
* ambiguous → evidence `needs_review` with the reason, no application touched;
* no match and `acknowledgement` → create the application (`created`, 1.0);
* no match and `follow_up`/`status_update` → evidence `needs_review`
  (`follow_up_without_application` / `status_update_without_application`). **Behaviour change:**
  Phase 1 created an `Applied` record and advanced it; that is exactly the duplicate source
  Phase 2 removes.

## 4. API (authenticated; CSRF and rate limits from Phase 1)

| Method & path | Purpose |
|---|---|
| `GET /api/v1/evidence` | filters: `linked`, `source`, `evidence_type`, `processing_status`, `date_from`, `date_to`, `page`, `page_size` |
| `GET /api/v1/evidence/counts` | `{unlinked, needs_review, total}` |
| `GET /api/v1/applications/{id}/evidence` | evidence linked to one application |
| `POST /api/v1/evidence/{id}/link` | body `{application_id}`; manual link, confidence 1.0 |
| `POST /api/v1/evidence/{id}/unlink` | back to `needs_review` (`manually_unlinked`) |

State-changing endpoints sit on the protected router (session + `X-CSRF-Token`) and use the
shared sensitive-operation rate limit. Responses never include `recipient`, `raw_metadata`
wholesale, OAuth material, or bodies — only a fixed summary (`classification`, `portal`,
`status_signal`). Unlinking does **not** revert status changes the evidence caused; those stay
in status history and can be corrected with the existing manual status update.

## 5. Migration strategy

Revision `0002_evidence_model` (after `0001_baseline`):

* creates `evidence` and its indexes; adds the five nullable `application` columns and three
  indexes — all "create if missing", so it is safe to re-run after the rollback stamp below;
* **backfill — prospects only.** Each `prospect` row is an email with a real Gmail message ID,
  thread, sender, subject (the prospect title), snippet and received date, so it becomes an
  `email`/`gmail` evidence row (`informational`, linked with `link_method=backfill` and
  confidence 1.0 when the prospect already points at an application). `raw_metadata.backfill`
  records the origin. Idempotent via `ON CONFLICT DO NOTHING` on the fingerprint.
* `last_evidence_at` is set from linked evidence where it is NULL.
* **Deliberately not backfilled:** `processedmessage` rows (only a message ID, a processing
  time and a result — no sender, subject or message date, so `occurred_at` would be invented),
  `statushistory.message_id` and `applicationevent.source_message_id` (same: the stored time is
  processing time, not message time), and `application.thread_ids` (thread-level, no message).
  These stay where they are; new mail creates evidence from now on.
* No existing row or column is modified (apart from the new columns being filled).
* `downgrade()` drops only the Phase 2 objects (evidence data is lost); the recovery path is
  still restore-from-backup, per `docs/database-operations.md`.

**Old-release compatibility.** The schema change is expand-only, so the Phase 1 code works on
it. Phase 1 deliberately refuses an unknown Alembic revision (maintenance mode), so rolling
code back to Phase 1 without restoring data requires stamping the version row back:
`python scripts/migrate_database.py stamp 0001_baseline` (guarded: maintenance flag + verified
backup first). Phase 1 then runs normally; evidence is simply not written while it does.
Re-deploying Phase 2 and upgrading again is safe because `0002` is idempotent, and runtime
maintenance refills the derived identity columns.

## 6. Rollback strategy

1. **Code only, keep data:** maintenance flag → `migrate_database.py stamp 0001_baseline`
   → deploy the Phase 1 image → remove flag. Evidence rows remain for when Phase 2 returns.
2. **Code and data:** restore the `pre-migration` backup `migrate_database.py upgrade` printed,
   then deploy Phase 1 (`docs/database-operations.md`, Rollback).
3. **A bad link:** `POST /evidence/{id}/unlink`, then relink; status corrections via the
   existing manual update.

## 7. Privacy constraints

* No message bodies are stored (unchanged). Snippets are Gmail's own preview, capped at 500.
* Non-job mail the poller sees is recorded as minimal `ignored` evidence (message ID, thread
  ID, date, fingerprint) — no sender, subject or snippet — so it is idempotent without keeping
  personal content.
* `recipient` is not stored for Gmail. API responses expose a fixed field set.
* Evidence is personal data: it lives in the same SQLite database, is covered by the Phase 1
  backups (0600) and is never logged (log lines carry IDs and statuses only).
* Tests use only synthetic fixtures and temporary databases under the Phase 1 isolation guards.

## 8. Acceptance criteria

- [x] Migration from the Phase 1 schema succeeds; models and migrations agree; existing
      application, status-history and event rows are byte-identical afterwards and analytics
      results are unchanged.
- [x] Importing/polling the same Gmail message twice yields one evidence row; fingerprints are
      deterministic across processes; concurrent inserts of the same evidence yield one row.
- [x] Several emails in one thread link to one application.
- [x] A follow-up, scheduling or rejection email with no matching application creates no
      application and is listed as `needs_review`; ambiguous evidence is listed too.
- [x] Link/unlink are transactional and keep `last_evidence_at` correct.
- [x] New endpoints require authentication; mutations require CSRF and are rate limited; no
      secrets, bodies or recipients are exposed.
- [x] Backup → verify → restore drill passes on the new schema; Phase 1 code runs on the
      expanded schema after the documented stamp.
- [x] No migration step is destructive; no production system is accessed.

## 9. Verification record

* Real Phase 1 code (`a579969`, exported with `git archive`) refuses the `0002` database
  (unknown revision → maintenance), and after `migrate_database.py stamp 0001_baseline` opens
  it, reads, writes and passes its diagnostics; re-upgrading with Phase 2 is a no-op for the
  schema and fills the derived columns of the row Phase 1 wrote.
* Backup → verify → restore drill on the Phase 2 schema restores all nine tables
  row-for-row, including evidence.
* Ingestion, migration, API and drill tests run under the Phase 1 isolation guards: no
  keychain, Gmail, network or real database access.

## 10. Open decisions

* **Review UI.** The review queue is API-only in this change; a frontend panel is follow-up.
* **Undoing status side effects.** Unlinking evidence does not roll back a status change it
  triggered; Phase 2's merge/undo work (plan Step 6) is the natural place for that.
* **Portal job IDs.** `external_job_id` exists and is matched by nothing yet; portal imports
  and the browser agent will populate it.
* **Lookback window.** Identity matching still considers only applications from the last
  180 days (`IDENTITY_LOOKBACK_DAYS`), as in Phase 1; older applications receiving mail will
  surface as `needs_review` rather than being linked.
