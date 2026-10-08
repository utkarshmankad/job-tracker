# Phase 1 — Trustworthy identity

Status: planned. Branch: `feat/phase-1-trustworthy-identity`. Baseline: `origin/main` @ `e674e01` (2026-10-09).

Goal: every application in the tracker corresponds to exactly one real job application, every
email or portal record that touched it is traceable, and no change to identity (create, link,
merge) is silent, unaudited, or irreversible. The deployed API is not usable by anyone except
the owner, and the database can be restored to a known point at any time.

## 1. Scope

1. **Protect the production API.** Every `/api/v1` route on the Fly deployment requires
   authentication. Fail closed when no secret is configured on a non-local host.
2. **Reliable backups and restore tooling.** Consistent online SQLite backups, integrity
   verification, retention, and a tested restore path, locally and on Fly.
3. **Versioned migrations.** Alembic becomes the only schema-change mechanism, replacing
   `SQLModel.metadata.create_all()` + `DataStore._migrate_schema()`.
4. **Evidence separate from canonical applications.** Gmail messages and portal records
   (LinkedIn paste, Excel import, manual entry) are stored as evidence rows that link to an
   application, instead of being folded into it.
5. **Better Gmail identity resolution.** An ordered, explainable resolver using strong keys
   first (thread, portal job ID, canonical job URL) and recording how and how confidently each
   piece of evidence was linked.
6. **Follow-up emails never create applications.** Only an application-confirmation email (or
   an explicit user action) creates a record. Status updates, reminders, and recruiter
   follow-ups with no match land in a review queue as unlinked evidence.
7. **Arbitrary multi-record merging.** Merge N records into one survivor in a single operation.
8. **Auditable, reversible merges.** Every merge is recorded with a full pre-merge snapshot and
   can be undone.
9. **Safe reconciliation of existing records.** A dry-run-first tool that backfills evidence,
   flags records created by follow-ups, and proposes merges that the user approves.
10. **Critical E2E coverage.** Repair the existing Playwright suite and cover auth, merge/undo,
    and review-queue flows; run it in CI.

## 2. Out of scope

- Multi-user accounts, roles, or OAuth login for the dashboard (single owner only).
- Moving off SQLite (Postgres, Turso, etc.).
- Off-provider backup storage (S3/R2/GCS). Phase 1 relies on local + Fly volume backups; see §6.
- Dropping legacy columns/tables (`Application.thread_ids`, `ApplicationThreadId`). Phase 1
  migrations are additive only; contraction happens in a later phase after one stable release.
- Changes to the LLM extractor, analytics, insights, or the notification work in progress on
  other branches.
- Calendar or non-Gmail email sources.
- Paying down repository-wide ruff/ESLint debt beyond files Phase 1 touches.
- Visual redesign of the dashboard beyond what merge/undo and the review queue need.

## 3. Current baseline

Measured in a clean worktree of `origin/main` @ `e674e01` on macOS, using the project `.venv`
(Python 3.11) and Homebrew `mypy 1.10.0` / `ruff 0.4.4` (same versions as
`requirements-dev.txt`). `JOB_TRACKER_DIR` pointed at a scratch directory.

| Check | Command | Result |
|---|---|---|
| Backend tests | `pytest tests/unit tests/integration --cov=backend` | **329 passed**, 0 failed (101 s) |
| Backend coverage | same | **85%** (3224 statements, 483 missed); CI gate is 80% |
| Agent tests | `pytest tests/test_agent.py` | **Not collected** — `ModuleNotFoundError: anthropic` in local `.venv` (it is in `requirements.txt`). Not in `testpaths` or CI. |
| Frontend tests | `npx vitest run` | **34 passed** in 2 files |
| Mypy | `mypy backend/` | **1 error**, environment-only: `Library stubs not installed for "yaml"` (`types-PyYAML` missing from local `.venv`; installed in CI). No code errors in 30 files. |
| Ruff lint | `ruff check backend/ tests/` | **39 errors** (29 auto-fixable) in 10 files; pre-existing debt. CI lints changed files only. |
| Ruff format | `ruff format --check backend/ tests/` | **8 files** would be reformatted |
| ESLint | `npx eslint .` | **4 errors, 2 warnings** across 6 files (`set-state-in-effect`, unused var, `only-export-components`, exhaustive-deps). CI lints changed files only. |
| Frontend build | `npm run build` | **Succeeds**; warning: main chunk 710 kB (> 500 kB) |
| E2E | `pytest tests/e2e` (Playwright, chromium) | **1 passed, 6 failed**. Selectors are stale after the accessibility pass (`h1` is now `Dashboard`, nav/filter labels changed). Not run in CI. Fixtures use fixed `sleep()` and write `frontend/.env.local`. |

**Database migration approach.** None versioned. Startup runs `SQLModel.metadata.create_all()`
then `DataStore._migrate_schema()`, which checks for missing columns and issues
`ALTER TABLE ... ADD COLUMN` (`withdraw_reason`, `application_method`,
`prospect.application_id`), followed by ad-hoc backfills (`_backfill_application_events`,
`_backfill_thread_id_index`). Alembic is a dependency and scaffolded (`alembic.ini`,
`backend/db/alembic/env.py`) but unwired: `target_metadata = None`, placeholder
`sqlalchemy.url`, zero revisions. `backend/db/migrations/` is an empty placeholder. No backup
or restore tooling exists; SQLite runs in WAL mode with `foreign_keys=ON`.

**API authentication.** Effectively none. The backend is deployed publicly on Fly
(`job-tracker-api-verdant-haze-8797`, `force_https`), and all 35 routes — including
`DELETE /applications/{id}`, `/applications/bulk-delete`, `/applications/duplicates/merge`,
`/poller/trigger`, `/diagnostics`, and `/export` — are reachable without credentials. CORS
restricts browsers only, not direct callers. The single guarded route,
`GET /poller/reauth/start`, checks `if ADMIN_TOKEN and token != ADMIN_TOKEN`, so it is **open
when `ADMIN_TOKEN` is unset**, and the token travels in the query string. The frontend
(`frontend/src/api/client.js`) sends no credentials.

**Identity today.** `StatusUpdater.process()` looks up by Gmail thread ID, then
`DuplicateDetector.find_duplicate()` (exact canonical job URL → single same-company record when
the email carries a status signal → rapidfuzz `company role` ≥ 85 within the lookup window).
Anything unmatched goes to `_create_new()`, so a rejection or interview email on a new thread
with an unmatched company spelling creates a new `APPLIED` record. Gmail threads are stored
twice (`Application.thread_ids` JSON and `ApplicationThreadId`). Merge is pairwise
(`DataStore.merge_applications`), hard-deletes the duplicate, writes `current_status` directly
(bypassing `StatusUpdater._advance_status()`, contrary to the architecture rules), and keeps no
audit record, so it cannot be undone.

## 4. Implementation sequence

Each step is one PR into `feat/phase-1-trustworthy-identity` (or directly to `main` when it
stands alone), merged only when the full suite, mypy, and the changed-file lint gates pass.
Order is chosen so the riskiest exposure (open API) closes first and every schema change
happens after backups and migrations exist.

### Step 0 — Test harness repair

- Fix stale E2E selectors; replace fixed `sleep()` with health polling; pass the API base via
  environment instead of writing `frontend/.env.local`; bind to free ports.
- Add an `e2e` CI job (`playwright install --with-deps chromium`) — non-blocking until Step 8.
- Document required local dev packages (`anthropic`, `types-PyYAML`, `ruff`, `mypy`) in
  `SETUP.md`.

### Step 1 — Protect the production API

Implemented; see [`docs/authentication.md`](authentication.md). Final design (changed from the
original API-token sketch in favour of Google identity):

- Google Identity Services in the browser → `POST /auth/google` with the ID token → server-side
  verification (signature, issuer, audience, expiry, single-use nonce, verified email equal to
  `AUTH_ALLOWED_EMAIL`) → HMAC-signed `HttpOnly`, `SameSite=Lax` session cookie (`__Host-`,
  `Secure` in production) plus an in-memory CSRF token required on state-changing requests.
- `backend/api/auth.py` holds the single `require_user` dependency; `backend/main.py` mounts
  the whole data router with it. Public surface: `/health` and `/auth/*` only.
- Vercel rewrites `/api/*` to Fly, so production is same-origin; CORS is an explicit
  credentialed allow-list for local development.
- `APP_ENV=production` (Docker image, `fly.toml`) fails closed at startup without complete
  `AUTH_*` configuration. `AUTH_MODE=local` is an explicit, loopback-only developer mode,
  refused in production. `ADMIN_TOKEN` removed; Gmail re-auth `state` is bound to the session
  and expires; the callback requires the session (via `PUBLIC_BASE_URL` = Vercel origin).
- In-process rate limits on sign-in and sensitive operations; consistent `{"detail", "code"}`
  401/403/429 bodies; query strings redacted from access logs.
- Frontend: login screen, session restore, account display, sign-out, 401/expiry handling.

### Steps 2–3 — Versioned migrations and verified recovery

Implemented together; the operator runbook is [`docs/database-operations.md`](database-operations.md).

**Migration design**

- Alembic is wired (`backend/db/alembic/env.py`: `SQLModel.metadata`, `render_as_batch=True`,
  URL from `DB_PATH` or `-x db=`; programmatic use shares DataStore's connection).
- Revision `0001_baseline` reproduces the `origin/main` schema exactly (a test compares
  `sqlite_master` DDL with a dump taken from `origin/main`) and is idempotent: it creates an
  empty database, adopts an unversioned `origin/main` database without changing any object,
  and completes older databases (missing tables/columns/indexes and the `Instahire` data fix)
  exactly as `create_all()` + `_migrate_schema()` used to. It refuses to downgrade.
- `DataStore._migrate_schema()` and `create_all()` are gone. `backend/db/schema.py` applies a
  startup policy: empty → create at head; outdated → in development back up then upgrade
  (`DB_AUTO_MIGRATE`, default on), in production refuse with `SchemaOutdatedError`; unknown
  revision → refuse. A refused database puts the API in **maintenance mode** (no DataStore, no
  poller, data endpoints 503, health unchanged). Diagnostics use an inspect-only policy.
- Idempotent runtime data maintenance (poller-state row, thread-ID and milestone-event
  backfills) still runs at startup, but only on a current schema.
- Schema-version diagnostic: `scripts/migrate_database.py status`, the `schema_revision` and
  complete `schema_tables` checks in `backend/diagnostics.py`, and a *Database Schema*
  component on the authenticated `/status`.

**Backup strategy**

- SQLite online backup API only (`DataStore.online_backup`), never file copies; the copy is
  converted to a standalone file, integrity-checked, and described by a manifest (timestamp,
  schema revision, SHA-256, size, application and per-table counts, tool version) written
  last. Directories are created exclusively; nothing is ever overwritten. Files 0600.
- `scripts/backup_database.py` (verifies by default; `--keep N` prunes only after a verified
  backup and only tool-written backups). Pre-migration backups are automatic and mandatory in
  `scripts/migrate_database.py upgrade`.
- Fly: backups on `/data/backups`; opt-in daily GitHub Actions workflow over `fly ssh`
  (`FLY_BACKUPS_ENABLED`), Fly volume snapshots, and manual off-provider pulls.

**Restore procedure**

`scripts/restore_database.py <backup> --destination <file> [--force]`: validates manifest,
size, checksum and `PRAGMA integrity_check`; builds the copy beside the destination,
re-checks integrity, revision and application count; then atomically moves it into place.
An existing destination (or `-wal`/`-shm`) is refused without `--force`, and with `--force`
moved aside to `*.pre-restore-<ts>`. The live `DB_PATH` additionally requires the maintenance
flag (or `--allow-live` with the API stopped). `scripts/verify_backup.py` restores into a
temporary directory, trial-migrates older backups there, and opens the copy through DataStore.

**Rollback procedure** — see §5 and the runbook: restore the printed pre-migration backup
while in maintenance mode, then deploy the matching release; or roll forward.

**Operational risks** — backups share the Fly volume (mitigated by snapshots and off-provider
copies); SQLite DDL is not fully transactional (mandatory backup, trial migration, post-checks);
maintenance windows pause Gmail polling (history ID catch-up; backfill after ~7 days);
restores lose post-backup manual edits; backups contain personal data. Full table in the
runbook.

### Step 4 — Evidence model (additive migration `0002`)

New SQLModel tables (schema source of truth stays `backend/db/models.py`):

- `email_evidence` — `gmail_message_id` (unique), `gmail_thread_id`, `sender`,
  `sender_domain`, `subject`, `received_at`, `snippet`, `email_kind`
  (`application_confirmation` | `status_update` | `follow_up` | `digest` | `prospect` |
  `unknown`), extracted `company`, `role`, `job_url`, `portal_job_id`, `status_signal`,
  `parser_version`, nullable `application_id`, `link_method` (`thread` | `portal_job_id` |
  `job_url` | `company_role` | `company_only` | `manual` | `reconciliation`),
  `link_confidence`, `linked_at`, `created_at`. **No body text**, per the architecture rules.
- `portal_evidence` — `source` (`linkedin_paste` | `excel_import` | `manual`), `external_ref`,
  extracted fields, nullable `application_id`, link metadata as above.
- `application_identity_key` — `application_id`, `key_type` (`gmail_thread` |
  `portal_job_id` | `job_url`), `key_value`; unique on (`key_type`, `key_value`) so one strong
  key can never point at two applications.

Poller dual-writes evidence alongside the existing path; `ProcessedMessage` stays the idempotency
ledger. A data migration backfills evidence/keys from `thread_ids`, `ApplicationThreadId`,
`StatusHistory.message_id`, `ApplicationEvent.source_message_id`, and `Prospect`.

### Step 5 — Identity resolver and follow-up handling

- New `backend/engine/identity_resolver.py`, replacing `StatusUpdater._find_existing` and the
  matching half of `DuplicateDetector`. Ordered rules, first hit wins, each returning
  `(application, link_method, confidence)`:
  1. Gmail thread ID key.
  2. Portal job ID extracted from email (LinkedIn/Naukri/ATS job IDs) key.
  3. Canonical job URL key.
  4. Normalized company (legal suffixes, ATS sender display names such as Greenhouse/Lever/
     Workday mapped to the employer) + normalized role, fuzzy ≥ threshold, among non-terminal
     applications in the lookup window.
  5. Company-only, **only** when exactly one active application exists for that company and the
     email is a status update; recorded with low confidence and surfaced for confirmation.
- Email classification adds `email_kind`. Only `application_confirmation` may create an
  application. `status_update`/`follow_up`/`unknown` with no match are stored as unlinked
  evidence and appear in a **review queue** (`GET /evidence/unlinked`,
  `POST /evidence/{id}/link`, `POST /evidence/{id}/create-application`, `POST /evidence/{id}/dismiss`).
- Status changes from linked evidence still go only through `StatusUpdater._advance_status()`.
- Parser rules for follow-ups (reminders, "complete your application", assessment nudges,
  recruiter check-ins, "we received your message") added to `portal_rules.yaml` with anonymized
  fixtures under `tests/fixtures/emails/`.

### Step 6 — Auditable, reversible N-way merge (additive migration `0003`)

- `Application` gains `merged_into_id` (nullable FK) and `merged_at`. Merged records are
  soft-deleted: excluded from every list/analytics query, never hard-deleted by a merge.
- `merge_operation` table: `id`, `survivor_id`, `absorbed_ids` (JSON), `snapshot` (JSON of
  every affected row before the merge — survivor, absorbed applications, and the IDs of each
  relinked status-history, event, prospect, evidence, and identity-key row), `reason`,
  `created_at`, `undone_at`.
- `DataStore.merge_applications(survivor_id, absorbed_ids, reason)` runs in one transaction;
  survivor status is recomputed through `StatusUpdater` (fixing the current direct
  `current_status` write).
- `POST /applications/merges` (2..N IDs), `GET /applications/merges`,
  `POST /applications/merges/{id}/undo`. Undo restores from the snapshot in one transaction;
  returns `409` with a description if later changes conflict (e.g. new evidence linked to the
  survivor that belongs to an absorbed record is left on the survivor and reported).
- The existing pairwise `POST /applications/duplicates/merge` becomes a thin wrapper, then is
  deprecated.
- Frontend: multi-select merge from the applications table and duplicate panel, survivor
  picker with field-level preview, merge history with undo.
- Open decision: undo needs to restore a previous (possibly "earlier") status. Proposal: a
  dedicated `StatusUpdater` restore path that writes a `StatusHistory` row with
  `trigger="merge_undo"`, keeping the single-writer rule intact.

### Step 7 — Reconcile existing records

- CLI `backend/db/reconcile_cli.py` with `--dry-run` (default) and `--apply <plan.json>`.
- Dry run (read-only DB; Gmail metadata fetch through `GmailPoller`, `format=metadata`, rate
  limited) produces a plan and human report:
  - applications whose creating message classifies as non-confirmation (follow-up-created);
  - duplicate clusters by strong key, then by resolver rule 4;
  - evidence that resolves to a different application than today's link;
  - conflicts that need a human decision.
- Apply: automatic backup first; executes approved merges through the Step 6 merge path (so
  every change is reversible), relinks evidence, and marks follow-up-created records for review
  rather than deleting them. Idempotent; re-running on an applied plan is a no-op.
- Run against a copy of the production DB first; compare counts and analytics totals before and
  after.

### Step 8 — Critical E2E coverage

Make the E2E job blocking in CI. Flows, all against a seeded temporary DB with Gmail mocked:

1. Unauthenticated access is rejected; login → dashboard loads; logout.
2. Manual add → appears in table.
3. Select three records → merge → one survivor with combined history → undo → three records back.
4. Unlinked follow-up evidence appears in the review queue → link to an application → status
   advances via history.
5. Backup → restore round trip via CLI (integration-level, not browser).

## 5. Migration and rollback strategy

Concrete procedures (production migration sequence, rollback, Fly backup/restore) are in
[`docs/database-operations.md`](database-operations.md). Summary:

- **Expand only.** Every Phase 1 revision adds tables/columns/indexes; nothing is dropped or
  renamed. The pre-Phase-1 release runs against any Phase 1 schema. A Phase 1 release whose
  head is older than the database's revision deliberately starts in maintenance mode, so a
  code rollback across a migration is paired with restoring the pre-migration backup (or a
  roll-forward).
- **Backup before change.** `migrate_database.py upgrade` always creates and verifies a backup
  (including a trial migration of a temporary copy) before migrating; development
  auto-upgrades take one too; reconciliation apply (Step 7) will do the same. Backups carry
  their Alembic revision.
- **Restore, not downgrade.** `0001_baseline` refuses to downgrade. Later revisions provide
  downgrades where they are safe, but the primary recovery path is restoring the
  pre-migration backup, which the migration command prints.
- **Dual-write before cut-over.** Evidence is written alongside the legacy thread fields
  (Step 4) before the resolver reads from it (Step 5). Legacy fields remain authoritative until
  the resolver ships; removal is a later phase.
- **Production rollout per step:** quiesce (maintenance flag) → `migrate_database.py upgrade`
  → diagnostics → resume → confirm `/status` and the poller → spot-check data. Rollback:
  restore the pre-migration backup in maintenance mode, then `fly releases` → redeploy the
  matching image.
- **Merges are reversible by design** (soft delete + snapshot), so reconciliation mistakes are
  undone per operation without a full restore.
- **Auth rollout:** set the `AUTH_*`, `FRONTEND_ORIGIN` and `PUBLIC_BASE_URL` Fly secrets and
  remove `VITE_API_BASE` from Vercel before deploying Step 1, so the fail-closed check does
  not take the API down (see `docs/authentication.md`). Rollback: redeploy the previous
  release (never ship a "disable auth" flag to production).

## 6. Security risks

| Risk | Today | Mitigation in Phase 1 |
|---|---|---|
| Public unauthenticated API exposes job-search PII (companies, roles, senders, snippets) and allows deletes, merges, bulk edits, poller triggers | Fixed by Step 1 once deployed | Router-level auth, fail closed |
| `reauth/start` open when `ADMIN_TOKEN` unset → attacker could bind their own Gmail account | **Live** if secret unset in Fly | Moves under required auth; startup check |
| Secrets in URLs (`?token=`) captured by logs/proxies/history | Live on `reauth/start` | Header/cookie only |
| Token shipped in the frontend bundle via `VITE_*` | Risk of naive implementation | Same-origin proxy + HttpOnly cookie; no client-side secret |
| CSRF once cookies are used | New | `SameSite=Strict`, same-origin, state-changing routes require JSON content type; Bearer path unaffected |
| Brute-forcing the token | New | High-entropy token (≥ 32 bytes), constant-time compare, rate limit |
| Backup files contain all personal data | New | `0600`, inside `JOB_TRACKER_DIR`, gitignored and dockerignored, downloads only when authenticated |
| Restoring a tampered or wrong-version file | New | Integrity check, SHA-256 manifest, revision check, current DB moved aside not deleted |
| Reconciliation fetching Gmail data beyond policy | New | Read-only scope unchanged, `format=metadata`, no body text persisted, logs exclude snippets |
| Merge snapshots duplicate data | New | Stored in the same DB under the same protections; no external export |
| Single-slot, non-expiring OAuth `state` in `app.state` | Live | Add expiry; one outstanding state |
| Gmail OAuth token storage on Fly (keyring on Linux) | Unverified | Verify where the token lives on the Fly machine during Step 1; document |

## 7. Test strategy

- **Unit** (mirrors source per `CLAUDE.md`): `test_auth.py`, `test_identity_resolver.py`,
  `test_backup.py`, `test_schema.py`, `test_recovery_cli.py`, `test_reconcile_cli.py`, plus extensions to `test_data_store.py`,
  `test_status_updater.py`, `test_email_parser.py`. Resolver rules are table-driven, one case per
  rule and per known false-positive from the May 2026 parser fixes.
- **Email fixtures**: anonymized header/snippet fixtures for confirmations, status updates,
  follow-ups, digests per portal; a regression case for every follow-up type that previously
  created an application.
- **Migrations**: empty-DB upgrade equals models; round-trip up/down; upgrade of a fixture copy
  of the pre-Phase-1 schema (including legacy NAME-format enum rows) preserves row counts and
  content.
- **Integration** (`tests/integration/test_routes.py`): every route returns 401 without
  credentials when auth is required; N-way merge + undo through the API; review-queue endpoints;
  backup/restore drill.
- **Property-style invariants** checked after resolver and reconciliation tests: no identity key
  maps to two live applications; no merged application appears in list/analytics responses;
  every live application has at least one evidence row or a manual-creation history entry.
- **E2E**: Step 8 flows, blocking in CI.
- **Gates**: suite green, backend coverage stays ≥ 85% (do not regress baseline; CI floor 80%),
  `mypy backend/` clean, ruff/ESLint clean on every touched file.
- Gmail is always mocked with `unittest.mock`; no test calls the real API; all file I/O uses
  `tmp_path`.

## 8. Definition of done

- [ ] All Fly-deployed `/api/v1` routes except the documented liveness and sign-in
      routes return 401 without valid credentials; the server refuses to start in production
      without secrets; no secret appears in the frontend bundle or in URLs.
- [ ] Daily and pre-migration backups run on Fly and locally; `scripts/restore_database.py` has been
      exercised end to end against a copy of production data; Fly volume snapshots confirmed.
- [ ] Alembic is the only schema-change path; production DB is stamped and at `head`;
      `create_all()`/`_migrate_schema()` removed; `CLAUDE.md` updated.
- [ ] Every Gmail message and portal record that affects an application is stored as evidence
      with link method and confidence; no email body text stored.
- [ ] Non-confirmation emails cannot create applications (enforced and tested); unmatched ones
      are visible in the review queue.
- [ ] Users can merge any number of records in one operation, view merge history, and undo a
      merge; merges never hard-delete; status changes still go only through `StatusUpdater`.
- [ ] Reconciliation has been dry-run and applied to production with a pre-apply backup;
      before/after report saved; no unexplained count changes.
- [ ] Repaired and new E2E flows pass and gate CI.
- [ ] Full backend suite and frontend tests pass; coverage ≥ 85%; mypy clean; touched files
      ruff/ESLint clean.
- [ ] README/SETUP document auth setup, backup/restore, migrations, and the review queue.
