# Phase 3 — Read-only collection from job sites

Status: released (PR #19, merge `b319293`, schema `0005_source_collection`). Indeed is
live-verified and is the only source ready for controlled collection; every other site
adapter is unsupported (§7). Scheduling is optional and disabled. Operations runbook:
[`collector-operations.md`](collector-operations.md).

## Goal

Many applications (LinkedIn Easy Apply, Naukri, Indeed, Instahyre, CareerNet, employer
portals) never produce a reliable confirmation email, so Gmail ingestion misses them. A
local agent reuses a browser profile the user is already signed in to, reads each site's
own **application-history** page, and sends minimal normalized observations to Job
Tracker. The tracker resolves them against existing applications and Gmail evidence with
the Phase 2 resolver.

The agent is **observational only**. It never submits or withdraws an application,
sends a message, edits a profile, accepts an interview, changes an account setting, or
bypasses a CAPTCHA or other access challenge.

## 1. Architecture

```
your computer                                                 Job Tracker (Fly)
┌───────────────────────────────────────────┐                ┌──────────────────────────┐
│ scripts/collect.py (collector/ package)   │                │ /api/v1/collector/*      │
│  config.toml (no secrets)                 │  HTTPS, Bearer │  scoped collector auth   │
│  keychain: collector credential           │ ─────────────► │  contract validation     │
│  Playwright → your dedicated profile      │  observations  │  ObservationIngestor      │
│  adapters → validate → batch              │  (no cookies)  │   → evidence → resolver  │
│  lock, local content-free run log         │                │   → create/link/review   │
└───────────────────────────────────────────┘                └──────────────────────────┘
```

- **Local agent** (`collector/`, `scripts/collect.py`). Runs where the signed-in browser
  is. It is not part of the API image: the Dockerfile copies only `backend/` and
  `scripts/`, and the agent's dependencies are in `requirements-collector.txt`.
- **Backend**:
  - collector endpoints (`backend/api/collection.py`);
  - the shared observation contract (`backend/collection/contract.py`);
  - ingestion (`backend/collection/ingest.py`) and decisions
    (`backend/collection/resolution.py`);
  - storage (`backend/db/collection_store.py`, a `DataStore` mixin).
- Browser cookies and site credentials never leave the browser profile. The backend only
  ever sees observations and the collector's own scoped credential.

## 2. Data model (`0005_source_collection`, additive)

| Table | Purpose | Uniqueness |
|---|---|---|
| `collector` | a collector credential: name, public `token_id`, SHA-256 of the secret, source `scopes`, enrolled/last-used/rotated/revoked timestamps | `token_id` |
| `collectorenrollment` | single-use setup codes (hash), 10-minute expiry | `code_hash` |
| `collectionsource` | one source account (`source_key`, user-chosen `account_label`): last attempt, last success, last status, needs-attention reason | `(source_key, account_label)` |
| `collectionrun` | one visit of one source: status, versions, counters, error code, content-free diagnostics | `run_key` (collector-generated) |
| `collectionbatch` | each submitted batch and its stored result | `(run_id, batch_key)` |
| `sourceitem` | one application as a site records it: stable ID or fingerprint, latest normalized and raw status, first/last seen, linked application, decision, confidence, needs-attention | `(source_key, item_key)` |
| `sourceobservation` | immutable record of what was seen: contract/collector/adapter versions, extraction tier, `content_hash`, fingerprint, minimal payload, evidence row, decision and reason | `(source_item_id, content_hash)` |

**Provenance** runs both ways:
- observation → `evidence_id` → evidence → `application_id`;
- evidence `raw_metadata.collector` = run key, observation ID, item key, status, extraction;
- `sourceitem.application_id`.

The downgrade refuses while any observation exists, and is safe to re-run.

## 3. Identity and idempotency

- **Stable ID first.** `item_key = "id:<source_item_id>"` when the site exposes one: a
  data attribute or the job ID in the link (LinkedIn `/jobs/view/<id>`, Indeed `jk`,
  Naukri `data-job-id`, Instahyre and CareerNet application refs).
- **Fingerprint otherwise.** `item_key = "fp:" + sha256(...)[:40]` over
  `collector-fp-v1`, the source key, the normalized company and role, the applied date
  and the canonical URL, joined by U+001F. Status is excluded, so a status change is a new
  observation of the *same* item.
- `content_hash` covers company, role, applied date, status, raw status, URL and the
  submission flag. A re-observation with the same hash is `unchanged`: it only moves
  `last_seen_at`. A changed hash appends an observation and an evidence row; it never
  creates a second application.
- **Retries and replays.** Run start is idempotent by `run_key`. Batches are idempotent
  by `batch_key` per run: a replay returns the stored result with `Idempotent-Replay:
  true`. A never-seen batch older than 15 minutes is refused (replay window).
- **Overlap and concurrency.** Inserts are `ON CONFLICT DO NOTHING` plus a read, so the
  unique constraints decide. Evidence is *claimed* before deciding, so concurrent or
  overlapping runs decide each observation once. Four concurrent submissions of one new
  item produce exactly one application (tested).
- **Interrupted runs.** An observation whose decision never completed stays `pending` and
  is resumed when the same content is submitted again.

## 4. Collector authentication

- **Credential:** `jtc_<16-hex token_id>.<43-char secret>`.
  - The secret is 256 bits from `secrets`.
  - The server stores `sha256("job-tracker:collector-secret:v1:" + secret)` and compares
    it in constant time.
  - The agent stores the credential in the OS keychain (`keyring`, service
    `job-tracker-collector`).
- **Setup:**
  1. A signed-in user creates a collector (name and source scopes) on the Sources page.
  2. The response contains a single-use setup code (10 minutes, stored hashed) and a
     copyable `scripts/collect.py enroll` command.
  3. The CLI exchanges the code at `POST /collector/enroll` (rate-limited per client) and
     writes the credential straight to the keychain.
  4. The UI never displays a long-lived secret. Collectors are listed with a
     `jtc_abcd…` hint only.
- **Scope.**
  - The credential is accepted *only* by `collector_router` endpoints: start and finish
    runs, submit batches for in-scope sources, read its own runs and metrics.
  - Every other endpoint authenticates the browser session cookie (and CSRF), so the
    credential cannot list, edit, delete or merge applications, decide evidence or manage
    collectors.
  - A route-classification test fails if any new route is unclassified.
  - Every collector route rejects anonymous and session-only callers.
- **Rotation** clears the secret hash immediately and issues a new token ID and setup
  code. **Revocation** is permanent and kills pending codes. Every failure returns one
  identical 401.
- **Audit:** structlog events `collector_created`, `collector_enrolled`,
  `collector_rotated`, `collector_revoked`, `collector_auth_failed`,
  `collector_scope_denied`, `collector_run_started` / `_finished`,
  `collector_batch_ingested`. They carry IDs, the public token ID, scopes and counts,
  never secrets or setup codes (tested).

## 5. API

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /collector/enroll` | setup code | exchange a code for the credential (once) |
| `GET /collector/me` | collector | identity and scopes |
| `POST /collector/runs` | collector | start or resume a run (`run_key`, `source_key`, `account_label`, versions); refused (403) for a source in scope that is no longer supported |
| `POST /collector/runs/{run_key}/observations` | collector | idempotent batch (≤100 observations, ≤256 KB) |
| `POST /collector/runs/{run_key}/finish` | collector | final status, items seen, fixed error code, counters |
| `GET /collector/runs/{run_key}` | collector | run status |
| `GET /collector/metrics` | collector | own aggregate counts |
| `GET /collectors` | session | list collectors |
| `POST /collectors` | session + CSRF | create; returns setup code and command once; unsupported scopes rejected (422) |
| `POST /collectors/{id}/rotate` | session + CSRF | invalidate secret; new setup code; refused (409) while the scope holds an unsupported source — revoke instead |
| `POST /collectors/{id}/revoke` | session + CSRF | permanent revocation |
| `GET /collection/source-catalog` | session | which sources may be in a collector's scope, with the reason for each unsupported one |
| `GET /collection/sources` | session | sources with last attempt/success/attention |
| `GET /collection/runs[?source_key]` | session | recent runs |
| `GET /collection/runs/{id}` | session | run with its observations and decisions |
| `GET /collection/metrics` | session | aggregate counts |
| `GET /collection/review` | session | collected items waiting for a person |

**Validation.** The contract forbids extra fields and enforces length limits. It requires
https URLs and strips tracking and query parameters (except known job-ID parameters),
credentials and fragments. It redacts email addresses and phone numbers from text, and
bounds `observed_at` (no future, at most 400 days old). Errors report field locations and
types, never submitted values. Run diagnostics accept integers, booleans and fixed codes
only, and the server supplies all user-facing error text. Rate limits: 120 requests a
minute per collector; enrollment 10 per 5 minutes per client.

## 6. Resolution and review (`backend/collection/resolution.py`)

For each new observation, after claiming its evidence:

1. **Known source item.** If any earlier observation of the item was linked, by the
   resolver or by a person in review, link to that application. Merges are followed to
   the survivor. The site's status goes through `StatusUpdater` (valid forward
   transitions only, history trigger `collector`).
2. **Item already in review.** The new observation joins the queue (reason
   `source_item_pending_review`).
3. **Phase 2 resolver**, with unchanged thresholds:
   - **`linked`** is accepted only if `verify_link` passes: no conflicting job ID, URL,
     company, role, date or source, and an active, unmerged target. If the extraction is
     not `verified`, it is accepted only when a strong identifier (same job ID or
     canonical URL) names the application. Otherwise the item goes to review.
   - **`new_application`** creates an application only if the row proves submission
     **and** the extraction is `verified`. Otherwise the item goes to review.
   - **Review reasons** go to the existing queue (`/evidence/review`, and
     `/collection/review` with the item's fields). A person decides with the existing
     accept, create-application or dismiss actions. That decision then governs every
     later observation of the item.

Never: automatic merges, overwriting a person's decision, or silently overwriting an
application's fields.

## 7. Adapters and real-session readiness

Validated read-only on 2026-10-09 in the user's own signed-in browser. Only the
application-history area was visited, and nothing was clicked except a read-only filter.

<!-- readiness-table: kept in sync with collector/adapters/sites.py and
     backend/collection/readiness.py by tests/unit/test_source_readiness_consistency.py -->

| Source | Readiness | Evidence |
|---|---|---|
| `indeed` (My jobs → Applied) | **live verified 2026-10-09** — the only source ready for controlled collection | selectors rewritten from the live page (stable `data-testid` and ARIA hooks); 4/4 rows read, matching the site's own "4 Applied"; two runs gave identical identities and hashes; stable `jk` IDs; dates parsed. Production canary: one bounded batch, repeated once, idempotent |
| `linkedin` | **unsupported** | the new Job Tracker layout (`/jobs-tracker/?stage=applied`) lacks safe stable row boundaries: generated class names, no row markers, job links outside rows |
| `naukri` | **unsupported** | until item identity, applied dates and complete inner-scroll collection can be established: real page is `/myapply/historypage`; cards have no item ID or applied date, ~7 of N render (inner scroll), "external site" applies aren't proof of submission |
| `instahyre` | **unsupported** | no application-history page; "Activity" lists recruiters who viewed the résumé (with their names) |
| `careernet` | **unsupported** | candidate platform is a separate site (mycareernet); history page not located; signed out there |
| `employer-<slug>` | only with an explicit local definition; `unverified` until its `verified_on` is set | — |

Unsupported sources cannot be put in a collector's scope (the API rejects them, and the
Sources page shows them disabled with the reason). Bringing a source back, or verifying
any new adapter, is the generic supervised process in
[`collector-operations.md`](collector-operations.md) §5.

**What the validation proved.** Every wrong guess failed safe: `unexpected_page`, never a
false empty success. It also found and fixed:
- the driver's dependence on "network idle";
- tab loss during sign-in;
- reading client-rendered lists too early;
- a Naukri unknown-route page that would have been misreported as signed out;
- dates shown without a year.

An adapter can declare the site's own count (Indeed's "<n> Applied"). Reading fewer
unique items than that ends a run `partial` (`incomplete_history`).

**Session persistence (operational limit).** A dedicated Playwright-driven profile did
not keep Indeed or Naukri sign-ins across browser restarts (likely session-only cookies).
Scheduled runs may therefore report `signed_out` until you sign in again with
`open-profile`. The collector reports this and never works around it. Persistence must
be shown (a manual run after a full browser restart) before a schedule is enabled
([`collector-operations.md`](collector-operations.md) §6).

**Selector discipline:**
- An adapter's primary selectors yield `extraction="verified"` only when `LIVE_VERIFIED`
  records a real-session check; otherwise `unverified`, which never auto-creates.
- Fallback sets are labelled `fallback`.
- A list the selectors cannot read, or a missing pagination control, stops with
  `selector_drift`.
- Signed-out, challenge, consent and rate-limit pages, unknown pages and redirects to
  other hosts all stop the run.

**Employer portals** need an explicit `[sources.employer]` definition. **Fixtures**
(`tests/fixtures/collector/`) are synthetic; Indeed's mirror the live structure.

## 8. Privacy, secrets and retention

**What leaves the browser:** company, role, applied date, site status (normalized and its
label), the site's item ID, a canonical job link, the submission flag, the extraction
tier and versions. Never page text, messages, recruiter names, email addresses, cookies,
account identifiers or HTML.

**Retention rules:**

| Data | Where | Kept |
|---|---|---|
| Observation payload (fields above) | tracker DB | as provenance for as long as the item exists; never raw HTML |
| Run diagnostics (counters, codes) | tracker DB | with the run |
| Collector secret | keychain (agent); hash only (server) | until rotated or revoked |
| Setup code | hash only | 10 minutes, single use |
| `runs.jsonl` (content-free) | agent state dir, 0600 | until `clear-diagnostics` |
| Dry-run files (validated observations) | agent state dir, 0600 | until `submit` or `clear-diagnostics` |
| Page snapshots | agent state dir, **off by default**; Fernet-encrypted, key in keychain, 0600 | at most 72h (default 24h), purged on each run; `clear-diagnostics` deletes them and the key |

The browser profile is referenced by path. It is never copied, never inside the
repository (refused by config), and an everyday Chrome profile is refused unless the user
opts in.

## 9. Security and source-site constraints

- **Your session, your terms.** The collector uses your normal authenticated browser
  session. You are responsible for complying with each site's terms and permissions.
- **Low frequency, human pace.**
  - Randomized 4–9 s pauses (minimum 2 s enforced) between page actions.
  - A page limit (default 10, maximum 50).
  - At most daily if you enable the (optional, disabled) schedule template.
- **No bypass.** CAPTCHAs, verification and rate limits are never bypassed: the run stops
  and asks you to resolve them in the visible browser.
- **No evasion.** No stealth plugins, fingerprint spoofing, CAPTCHA solvers, proxy
  rotation, credential interception, undocumented private APIs or evasion of any kind.
  The browser is visible (headless is off by default).
- **Maintenance.** Selectors will need maintenance, and sites can revoke or limit access
  at any time.
- **Stop on uncertainty.** The agent stops and reports with a code instead of guessing.

## 10. Known limits and risks

- Only Indeed is live-verified. LinkedIn, Naukri, Instahyre and CareerNet are
  unsupported; an employer definition stays `unverified` (new items go to review) until a
  person verifies it. Verification is a manual, supervised step
  ([`collector-operations.md`](collector-operations.md) §5).
- Scheduling is optional and disabled; nothing installs it.
- Status-label vocabularies are best guesses. Unknown labels map to `unknown`, which
  never proves submission or changes status.
- Fingerprint items (no site ID) whose company or role text changes on the site become a
  new item, which the resolver then matches or sends to review.
- The in-memory rate limiter resets on restart (single Fly machine, as in Phase 1).
