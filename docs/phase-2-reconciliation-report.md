# Phase 2 reconciliation and release-safety audit

Status: dry run against an isolated copy of the production database, 2026-10-09.
Branch `feat/phase-2-identity-resolution`. Starting commit `fd3befd`.

**Redacted.** This report holds aggregate counts only. It contains no names, company names, job
titles, email addresses, subjects, snippets, or message or thread IDs. The record-level
review plan stays outside the repository (see §9).

**Recommendation: ready for release.** All release gates pass (§11). No code blocker was
found. The conditions and residual risks are in §12.

## 1. What was done to production

Production ran the Phase 1 release at schema `0001_baseline`. Only two kinds of action
touched it:

- **Read-only checks:**
  - `migrate_database.py status`;
  - an application count over a `mode=ro` SQLite connection;
  - `df` and a backup listing.
- **One online backup and its verification:**
  - `backup_database.py --label phase2-audit` created backup `20261009T055712Z-phase2-audit` on the volume;
  - `verify_backup.py` verified it on Fly.

The audit did not migrate, link, merge, dismiss, update or delete any production record.
It did not poll Gmail, touch Gmail or Google auth, access the keychain, call an LLM, or
deploy anything.

## 2. Backup acquisition and verification

| Item | Value |
|---|---|
| Backup | `20261009T055712Z-phase2-audit` (online backup API, not a file copy) |
| Manifest SHA-256 | `492db893b4ca5f2bcdd4ac0a04e7a5d9dd3d1524996984020b86f9b305b24718` |
| Size | 1,187,840 bytes |
| Source schema | `0001_baseline` |
| Integrity | `ok` (manifest), verified on Fly and again locally |

**Download.** Only `applications.db` and `manifest.json` were downloaded, into a new
`mktemp -d` directory:
- directory mode 0700 (the `original/` subdirectory 0500);
- files mode 0400.

The local SHA-256 matched the manifest. The hash was re-checked after every step and
never changed.

**Local verification.** `verify_backup.py` ran locally with Phase 2 code. It restored the
backup to a temporary directory and opened it; the restored copy migrates cleanly to
`0004_merge_operations`.

**Production table counts:**

| Table | Rows |
|---|---:|
| application | 208 |
| statushistory | 228 |
| applicationevent | 197 |
| applicationthreadid | 178 |
| processedmessage | 9,233 |
| prospect | 0 |
| suppressrule | 28 |
| pollerstate | 1 |

## 3. Migration audit (0001 → 0004, working copy)

| Check | Result |
|---|---|
| Target revision | `0004_merge_operations` (head) |
| SQLite integrity | `ok` |
| Foreign-key check | 0 violations |
| Additive | yes; every pre-existing column of every table is byte-identical after the upgrade (`alembic_version` excluded) |
| Runtime maintenance on first open | changed nothing in pre-existing columns |
| New tables | `evidence`, `mergeoperation`, `duplicatedismissal` |
| Business aggregates | identical before and after |
| Active set | 208 → 208 (every application `active`) |
| Backup → verify → restore of the migrated copy | all tables identical |
| Original download | unchanged |

The business aggregates compared were applications by status, source and method, the
status-history count, and events by type.

**Counts after migration:**

| Table | Rows |
|---|---:|
| evidence | 0 |
| mergeoperation | 0 |
| duplicatedismissal | 0 |
| all other tables | as in §2 |

**Evidence is empty after migration.** Production has 0 prospects, and prospects are
the only historical source the migration backfills into evidence (by design, §5 of
[phase-2-identity-resolution.md](phase-2-identity-resolution.md)). Processed-message rows
hold no sender, subject or date, so they are deliberately not evidence. Evidence begins
with the first Phase 2 poll.

## 4. Evidence reconciliation

The deterministic resolver (`2.0.0`, thresholds auto-link 80, margin 25, review 40) ran over
all eligible evidence.

| Outcome | Count |
|---|---:|
| Total evidence | 0 |
| Already linked | 0 |
| Unlinked | 0 |
| Automatically linkable | 0 |
| Likely new applications | 0 |
| Review required | 0 |
| Ignored or irrelevant | 0 |
| Conflicting strong identifiers | 0 |
| Human-confirmed links preserved | 0 |
| Idempotent duplicates skipped | 0 |
| Resolver errors | 0 |

Every breakdown (evidence type, source, outcome, confidence band, link method, age band)
is therefore empty. Because nothing was auto-linked, the production data cannot show
whether automatic links are correct. That evidence comes from the test suite (resolver,
ingestion and these reconciliation tests) and from monitoring after release (§12).

### 4.1 Resolver replay of application identities (simulation)

To exercise the resolver on real data, each of the 208 active applications was replayed
as the acknowledgement that created it. The replay used the application's own company,
role, job link, first thread, applied date and source. The resolver ran with that
application hidden from its candidates, which asks: "would this record be attached to a
different existing record?"

| Replay outcome | Count | Meaning |
|---|---:|---|
| Linked to another record | 0 | no record is confidently a duplicate of another |
| New application (credible acknowledgement) | 174 | distinct |
| Review, `possible_reapplication` | 12 | same company and role, outside the date window or after a terminal status |
| Review, `below_auto_link_threshold` | 15 | plausible match, not confident |
| Review, `ambiguous_candidates` | 5 | several similar records at one company |
| Review, `not_a_credible_new_application` | 2 | too little identity |

Confidence bands: 0.90–1.00: 166; 0.67–0.89: 8; 0.33–0.66: 20; below 0.33: 14.

## 5. Duplicate reconciliation

| Measure | Count |
|---|---:|
| Candidate pairs (score ≥ 70) | 0 |
| Connected groups | 0 |
| Previously dismissed pairs | 0 |
| Groups with conflicting job IDs, conflicting URLs, different roles, large date spans, or needing field choices | 0 |
| High-confidence review / medium / insufficient evidence / do-not-merge | 0 / 0 / 0 / 0 |

No automatic merge exists, and none was performed. The resolver replay found 22 distinct
record pairs worth a human look:

| Kind of pair | Pairs |
|---|---:|
| Ambiguous | 4 |
| Below threshold | 9 |
| Possible re-application | 9 |

These pairs are in the local review plan as `possible_duplicate` items. Each requires a
human choice, and the plan proposes no merge.

## 6. Data quality affecting identity (active applications)

| Signal | Count |
|---|---:|
| Role field reads like message text (greeting or scheduling phrase) | 21 |
| Role empty | 9 |
| No mail thread | 72 |
| With job URL / with extractable job ID | 8 / 6 |
| Thread IDs shared by more than one application | 0 |

**What the noisy roles mean.** They are Phase 1 extraction artefacts: applications created
from follow-up mail. That is the duplicate source Phase 2 removes, because unmatched
follow-ups now go to review.

**Effect on the resolver.** A noisy role makes it score a role conflict, which sends mail
to review rather than to a wrong link. Noisy roles also keep some true duplicates below the
suggestion threshold.

**Manual inspection.** A sample of review pairs was inspected locally.
- **False positives:** none. There were no automatic links to inspect, and no replay pair
  was linked.
- **False negatives:** several pairs at the same company are probably the same
  application but scored below 70. One cluster of follow-up–created records had noisy or
  empty roles; one pair differed only by a company suffix.
- **Correct separations:** most other pairs are different roles at the same company, and
  the resolver correctly kept them apart.

The thresholds were not loosened. Cleaning the 30 noisy or empty roles in Data Quality
would let the existing rules surface those duplicates.

## 7. Merge/undo simulation (disposable copies only)

Real pairs were sampled deterministically from each replay review category (seeded). One
synthetic three-record group was added. Each merge was executed and undone on a disposable
copy, and every copy was deleted after the audit.

| Category | Runs |
|---|---:|
| Replay pairs: ambiguous | 4 |
| Replay pairs: below threshold | 5 |
| Replay pairs: possible re-application | 5 |
| Synthetic group | 1 |
| **Total** | **15** |

| Check (every run) | Result |
|---|---|
| Active count | n → n−(size−1) → n |
| Ownership moved to survivor | 78 status-history, event and thread-link rows across the runs, returned on undo |
| Snapshot checksum | valid |
| Undo | every business table logically identical to before the merge |
| Foreign-key violations | 0 |
| Analytics after undo | identical to baseline |

**Moved items by kind:** evidence and opportunities 0 (none exist); history, events and
threads all moved and were restored.

**No real merged state was measured.** Merging every high-confidence group would have
been a no-op, because there are 0 such groups. The only analytics change merging can
cause is removing the merged record from totals, which each per-pair run confirmed by its
active count.

## 8. Analytics comparison

Production as served today was computed by the Phase 1 code (`a579969`) on a copy of the
original backup. The migrated state was computed by Phase 2 code on a migrated copy. Both
used the fixed clock 2026-10-09T06:00Z. The full payloads were byte-identical (same
SHA-256), covering:
- report funnel and channel/method performance;
- flow;
- 6-month conversions;
- 28-day pulse;
- rejection;
- status and source distributions;
- stale count;
- the status-history count.

| Figure | Original (Phase 1) | Migrated (Phase 2) | With auto-links approved | After merge undo |
|---|---:|---:|---:|---:|
| Active applications | 208 | 208 | 208 | 208 |
| Interviews (status) | 7 | 7 | 7 | 7 |
| Offers (status) | 2 | 2 | 2 | 2 |
| Interview conversion | 3.37% | 3.37% | same | same |
| Offer conversion | 0.96% | 0.96% | same | same |
| Applied in last 6 months | 207 | 207 | same | same |
| Stale | 135 | 135 | same | same |
| Status: Applied / Rejected / Withdrawn / Interview Scheduled / Interview In Progress / Resume Shortlisted / Offer | 142 / 28 / 29 / 4 / 1 / 2 / 2 | same | same | same |
| Source: Direct/Consultancy / LinkedIn / Direct/Unknown / Lever / Ashby / Greenhouse / SmartRecruiters / Naukri | 86 / 64 / 40 / 7 / 6 / 2 / 2 / 1 | same | same | same |
| Weekly application rate (last 4 weeks) | 2, 6, 3, 4 | same | same | same |

- **Channel performance** (total, interviewed, offered, response rate per source) is
  identical in every state.
- **"With auto-links approved"** is unchanged because there are 0 links to approve.
  Evidence links never change application rows by themselves, though a linked status
  update processed live can advance status.
- **Migration** changed no figure. No derived backfill altered pre-existing data.

## 9. Determinism and outputs

**Two independent runs.** `scripts/reconcile_database.py` ran twice on the same backup
(seed `phase2-reconciliation`, as-of 2026-10-09T06:00Z). These outputs were byte-identical
between the runs: `summary.json`, `summary.md`, `review-plan.json` and `id-map.json`.

**Within one run.** Evidence, replay and duplicate analyses are each computed twice and
compared; they matched.

**Where the outputs live.** Outputs were written outside the repository in the 0700 temp
directory, each file mode 0600.

**Leak scanner.** Before writing, the CLI scans its own output for:
- every stored company, role, job URL, thread or message ID, sender and subject value;
- email addresses.

It refuses to finish if anything matches. It found nothing.

**Review plan (local only, not committed).** `review-plan.json` uses stable anonymized
IDs (`APP-…`, `GRP-…`). Each of its 22 items records:
- reason and confidence;
- positive and negative signals;
- proposed action;
- `human_choice_mandatory: true`;
- the anonymized application IDs.

`id-map.json` maps the labels back to record IDs for the operator. It stays local with
the plan.

## 10. Reconciliation CLI

`scripts/reconcile_database.py`, built on `backend/db/reconcile_cli.py` and
`backend/engine/reconciliation.py`:

- **Dry-run by default.** The input is opened read-only: it is hashed, digested per table
  and copied with the online backup API. Every step runs on private temporary copies. A
  changed SHA-256 or table digest fails the run (exit 3).
- **Refusals** (exit 2):
  - the configured `DB_PATH` without `--production-ack`;
  - a resolver version other than the running one;
  - thresholds below the configured values;
  - output inside the repository (except the gitignored `.job-tracker/`), or a non-empty
    output directory.
- **Options:**
  - `--db`, `--output-dir`, `--dry-run/--apply`, `--report-mode redacted|full`;
  - `--seed`, `--resolver-version`;
  - `--auto-link-score`, `--auto-link-margin`, `--review-score`, `--duplicate-score`;
  - `--max-records`, `--sample-size`, `--as-of`, `--simulate-merges`.
- **Outputs:** JSON plan and summary plus a Markdown summary.
- **`--apply`** was not used in this audit. It also needs `--apply-confirm`. It requires a
  database at head, takes and verifies a backup first, and records only re-verified
  automatic evidence links. It never creates applications, merges records or changes
  status.
- **No network:** it needs no Gmail, Google or network access.

**Supporting changes:**
- `InsightsEngine` accepts a clock and `is_application_stale` accepts `now`, for
  reproducible figures; the API still uses real time.
- `DataStore` gains read-only primitives: column-restricted table digests, schema-agnostic
  aggregates, a foreign-key check, a sensitive-term list for the leak scanner, and a
  read-only source mode for `online_backup`.

## 11. Release gates

| Gate | Result |
|---|---|
| Migrations 0001 → 0004 work on the production copy | ✓ |
| Original backup unchanged | ✓ (SHA-256 re-checked) |
| Dry-run non-mutating | ✓ (input hash and table digests; working copy unchanged; tests prove byte identity) |
| Resolver decisions deterministic across two runs | ✓ |
| No human-confirmed decision overwritten | ✓ (none exist; dry-run writes nothing; apply path refuses human-owned evidence, tested) |
| No automatic link with a conflicting strong identifier | ✓ (0 links; checks enforced per link) |
| No merge executed on production | ✓ |
| Merge/undo simulations restore data exactly | ✓ (15 of 15) |
| Analytics unchanged by migration alone | ✓ (byte-identical, Phase 1 vs Phase 2 code) |
| Test-isolation guards effective | ✓ (suite runs under the Phase 1 guards; drills pass explicit paths) |
| No production-derived data tracked by Git | ✓ (staged files scanned against stored values before commit) |
| Full CI-equivalent suite passes | ✓ (§13) |

## 12. Blockers, risks and remaining work

**Blockers:** none.

**Risks:**
- **Automatic linking is unproven on production mail.** Nothing in today's data exercises
  it. After deploy, watch `GET /evidence/metrics` and the review queue for the first week.
  Treat any wrong automatic link as a stop-ship and use unlink.
- **Phase 2 shifts work to review.** Unmatched follow-ups and status mail now wait in
  review instead of creating records. Expect some review volume from day one; it replaces
  the duplicates Phase 1 created.
- **Undetected duplicates remain.** Roughly 30 records with noisy or empty roles hide
  probable duplicates below the suggestion threshold.

**Manual review workload:**
- 22 possible-duplicate pairs (4 ambiguous, 9 below threshold, 9 possible re-applications);
- 30 role fields to clean (21 noisy, 9 empty);
- 0 evidence items.

**Rollback readiness:**
- The pre-audit backups and today's verified backup are on the volume.
- The production upgrade takes its own verified pre-migration backup.
- Migration 0004's downgrade refuses while any merge is in effect.
- A code-only rollback uses `migrate_database.py stamp` (see
  [database-operations.md](database-operations.md)).

**Release conditions:**
- Follow the production migration sequence in [database-operations.md](database-operations.md),
  including a volume snapshot, maintenance mode and the scripted upgrade.
- Re-run this audit on a fresh backup if production data changes materially before
  deploy.

## 13. Test and verification results

| Check | Result |
|---|---|
| Backend unit, integration and agent tests | 874 passed |
| Coverage gate (80%) | 91.17% (`reconcile_cli` 97%, `reconciliation` 91%, `insights_engine` 94%) |
| mypy (`backend/`, `scripts/`) | clean, 50 files |
| Ruff check and format (changed files) | clean |
| Frontend tests | 68 passed |
| Production build | ok |
| ESLint (changed frontend files) | 0 errors, 1 warning that already existed |
| Chromium E2E | 10 passed |
| actionlint | clean |
| pip-audit / npm audit | no known vulnerabilities |
| Phase 1 → Phase 2 migration drill and backup/restore drill (synthetic) | passed; restored tables identical; undo on restored copy ok |
| Docker build and authenticated smoke (local auth inside the container) | ok |
| Reconciliation run twice | byte-identical outputs |
| `git diff --check` | clean |

The Docker smoke tested:
- health;
- local sign-in refused from a non-loopback client (403);
- inside the container: sign-in 200, preview, merge, active count 2 → 1, undo → 2, and a
  mutation without CSRF refused (403).
