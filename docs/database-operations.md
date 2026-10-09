# Database operations: migrations, backups, restores

Job Tracker stores everything in one SQLite file: `applications.db` in `JOB_TRACKER_DIR`.
In production that is `/data/applications.db` on the Fly volume `job_tracker_data_sin`.
This runbook covers schema migrations, backups, restores and rollback.

Never copy `applications.db` with `cp`, `scp`, or `sftp` while the app may be running. The
database runs in WAL mode, so recent writes can still sit in `applications.db-wal`, and a
plain file copy can be torn or out of date. Always use `scripts/backup_database.py`, which
uses SQLite's online backup API.

## Tools

All commands run from the repository root, or `/app` in the container. All of them take
`--help`.

| Command | What it does |
|---|---|
| `python scripts/migrate_database.py status` | Shows the schema revision. Exit code 0 means current, 1 means an upgrade is pending, 2 means the database is missing or has an unknown revision |
| `python scripts/migrate_database.py stamp <older-revision>` | Records an older revision without touching schema objects (code-only rollback on an expand-only schema). Downward only; backup first; needs the maintenance flag on the live DB |
| `python scripts/migrate_database.py upgrade` | Takes a pre-migration backup, verifies it (including a trial migration on a temporary copy), migrates, then runs an integrity check and diagnostics |
| `python scripts/backup_database.py [--label L] [--keep N]` | Creates a verified backup and optionally deletes the oldest ones so N remain |
| `python scripts/verify_backup.py <dir> \| --latest` | Restores into a temporary directory and opens the copy through DataStore |
| `python scripts/restore_database.py <dir> --destination <file> [--force]` | Validates the backup and restores it. Refuses to overwrite unless `--force` is given, which moves the existing files aside |
| `python -m backend.diagnostics` | Health report: schema revision, required tables, enum format, poller, Gmail credentials |
| `python scripts/reconcile_database.py --db <backup copy> --output-dir <private dir>` | Dry-run Phase 2 reconciliation audit on copies: migration, resolver, duplicates, analytics, merge/undo simulation. Never touches the input; refuses the configured `DB_PATH`. See [`phase-2-reconciliation-report.md`](phase-2-reconciliation-report.md) |

`--db` and `--backup-dir` default to `DB_PATH` and `<JOB_TRACKER_DIR>/backups`.

### Backup format

Each backup is a new directory that is never reused:

```
backups/20261009T031700Z-pre-migration/
  applications.db   standalone copy (no -wal/-shm), mode 0600
  manifest.json     written last; a directory without it is incomplete
```

`manifest.json` holds:
- `created_at`, `label`
- `schema_revision` (`null` for a pre-Phase-1 database)
- `sha256`, `size_bytes`
- `application_count` and per-table `table_counts`
- `integrity_check` (`"ok"`)
- `tool`, `tool_version`, `format_version`

Restore and verify reject a backup in any of these cases:
- the manifest is missing or malformed
- the checksum or size doesn't match
- SQLite `PRAGMA integrity_check` fails
- the restored copy doesn't match the manifest's revision and application count

### Maintenance mode (quiescing writers)

While `<JOB_TRACKER_DIR>/MAINTENANCE` exists, the API starts in maintenance mode:
- no DataStore is opened and the Gmail poller does not start, so nothing writes;
- authenticated data endpoints return `503`;
- `/api/v1/status` and `/api/v1/diagnostics` still work, so you can see why;
- `/api/v1/health` still returns `{"status":"ok"}`, so Fly keeps the machine up.

The API also enters maintenance mode by itself when the schema needs a migration it won't
run unattended. In production it never migrates by itself.

`migrate_database.py upgrade` and `restore_database.py` refuse to touch the configured
`DB_PATH` unless the maintenance flag exists. The exception is `--allow-live`, for when
you've stopped the API yourself (local development).

## Migration design

- Alembic (`backend/db/alembic/`) is the only way the schema changes. Revisions live in
  `backend/db/alembic/versions/` and are frozen: they don't import `backend/db/models.py`.
- `0001_baseline` is the schema `origin/main` produced (the old `create_all()` plus the
  `_migrate_schema()` ALTERs). It is idempotent:
  - on an empty database it creates everything;
  - on an unversioned `origin/main` database it changes nothing and only records the
    revision;
  - on older databases it adds the missing tables, columns and indexes exactly as the old
    startup code did, plus the `Instahire` → `Instahyre` data fix.
  It can't be downgraded; restore a backup instead.
- `0002_evidence_model` (Phase 2) adds the `evidence` table and five nullable `application`
  identity columns, backfills evidence from existing prospects, and sets
  `application.last_evidence_at`. It is additive and idempotent ("if missing" plus
  `ON CONFLICT DO NOTHING`), so it can be re-run after the rollback stamp below. Details:
  [`phase-2-identity-resolution.md`](phase-2-identity-resolution.md) §5.
- `0003_resolver_audit` adds resolver audit columns and indexes; it is additive and idempotent.
- `0004_merge_operations` adds soft-merge columns on `application`, `superseded_by_merge_id`
  on `statushistory`/`applicationevent`, and the `mergeoperation` and `duplicatedismissal`
  tables. It is additive and idempotent. Its downgrade **refuses while any application is
  merged**: undo those merges first, or restore a pre-merge backup. Rolling back code only
  (stamp `0003_resolver_audit`) is safe: an older release ignores the new columns. Merged
  records would then reappear in its lists, though, so undo merges first if that matters.
  Details: [`phase-2-identity-resolution.md`](phase-2-identity-resolution.md) §12.
- Startup policy (`backend/db/schema.py`):

  | Database state | Development (`DB_AUTO_MIGRATE=true`, default) | Production (`APP_ENV=production`) |
  |---|---|---|
  | Empty | Created at head | Created at head |
  | Current | Normal start | Normal start |
  | Unversioned or outdated | Verified backup to `<dir>/backups`, then upgrade | **Maintenance mode**; the operator runs the sequence below |
  | Unknown revision (newer code migrated it) | Maintenance mode | Maintenance mode |

- Runtime data maintenance stays at startup, but only once the schema is current: the
  poller-state row, the thread-ID index backfill, and the milestone-event backfill. All of
  it is idempotent.
- New schema changes: edit `models.py`, then run
  `alembic -c alembic.ini -x db=/tmp/scratch.db revision --autogenerate -m "..."`. Hand-edit
  the result, keep it additive (expand only) where possible, and add a test.
  `tests/unit/test_schema.py::test_models_and_migrations_agree` fails if models and revisions
  drift apart.

## Production migration sequence (Fly.io)

`APP` is `job-tracker-api-verdant-haze-8797`. Run each `fly ssh console` command from your
own terminal. Merging to `main` deploys automatically (`.github/workflows/fly-deploy.yml`).

First decide which case applies. A migration can only be run by an image that contains
its Alembic revision:

```bash
fly ssh console --app $APP -C "python /app/scripts/migrate_database.py status"
fly ssh console --app $APP -C "ls /app/backend/db/alembic/versions"
```

- **Case A — the revision is already in the deployed image.** `status` reports `outdated`
  and the revision file is listed. This happens, for example, after a restore of an older
  backup. Use [Case A](#case-a-migration-already-in-the-deployed-image).
- **Case B — the revision arrives with the incoming image.** The new revision is not in
  `versions/`, and `status` reports `current` against the old head. Running `upgrade` now
  would do nothing. Use [Case B](#case-b-migration-introduced-by-the-incoming-image).
  Every release that adds a file under `backend/db/alembic/versions/` is Case B. That
  includes Phase 2 (`0001_baseline` → `0004_merge_operations`).

### Case A: migration already in the deployed image

1. Volume snapshot and maintenance mode: steps 3–4 of Case B.
2. `fly ssh console --app $APP -C "python /app/scripts/migrate_database.py upgrade"`. This
   takes a verified backup, migrates a trial copy, migrates, then checks integrity and runs
   diagnostics.
3. Verification and resume: steps 8–12 of Case B.

### Case B: migration introduced by the incoming image

In production, the new image refuses to serve on an outdated schema and starts in
maintenance mode. So the order is **deploy, then migrate**, never the reverse.

1. **CI passes on the pull request.** Every job, including `All Checks Passed`, the browser
   E2E tests and the dependency audit. Do not merge with a failing or pending check.
2. **Application-level backup, verified.**
   ```bash
   fly ssh console --app $APP -C "python /app/scripts/backup_database.py --label pre-release"
   fly ssh console --app $APP -C "python /app/scripts/verify_backup.py /data/backups/<name>"
   ```
   Record the backup name and its SHA-256 (it is in `manifest.json`).
3. **Fly volume snapshot, confirmed.**
   ```bash
   fly volumes list --app $APP                      # note the volume ID
   fly volumes snapshots create <volume-id>
   fly volumes snapshots list <volume-id>           # wait until the new one is "created"
   ```
4. **Maintenance mode.** This stops Gmail polling and data writes. `/api/v1/health` stays OK.
   ```bash
   fly ssh console --app $APP -C "touch /data/MAINTENANCE"
   fly machine restart <machine-id> --app $APP
   fly logs --app $APP --no-tail | grep -E "app_started_in_maintenance_mode|poller_scheduler_stopped"
   ```
5. **Merge the pull request.** The deploy workflow builds and releases the new image. Do not
   trigger a second deploy by hand while it runs.
   ```bash
   gh run list --branch main --limit 3              # watch "Fly Deploy" for the merge commit
   ```
6. **Confirm the new machine is healthy and in maintenance mode.**
   ```bash
   fly status --app $APP                            # new release, checks passing
   fly ssh console --app $APP -C "ls /data/MAINTENANCE"
   fly ssh console --app $APP -C "python /app/scripts/migrate_database.py status"
   ```
   `status` must now report `outdated`, with the head set to the new revision.
7. **Run the documented migration.**
   ```bash
   fly ssh console --app $APP -C "python /app/scripts/migrate_database.py upgrade"
   ```
   The command takes and verifies its own `pre-migration` backup, migrates a trial copy,
   then migrates. It finishes with `Integrity: ok` and the diagnostics. Note the
   pre-migration backup name.
8. **Verify schema, integrity, foreign keys, row counts and invariants.** These are
   read-only.
   ```bash
   fly ssh console --app $APP -C "python /app/scripts/migrate_database.py status"
   fly ssh console --app $APP -C "python -c \"import sqlite3; c=sqlite3.connect('file:/data/applications.db?mode=ro', uri=True); print(c.execute('pragma integrity_check').fetchall(), len(c.execute('pragma foreign_key_check').fetchall()))\""
   ```
   - Compare every pre-existing table count with the `table_counts` in the step 2 manifest.
     They must be equal.
   - Check the release's invariants. For Phase 2: every application `active`, no merge
     operations, no evidence linked by the release.
   - If anything fails, **stay in maintenance mode** and follow
     [Rollback](#rollback). Do not continue.
9. **Server-side smoke checks** (still in maintenance mode):
   - `/api/v1/health` returns OK;
   - an unauthenticated `/api/v1/applications` returns 401;
   - local sign-in is refused (403);
   - an unauthenticated mutation is refused;
   - the diagnostics pass:
     `fly ssh console --app $APP -C "sh -c 'cd /app && python -m backend.diagnostics'"`.

   Authenticated data endpoints return 503 while the flag is present. That is by design.
10. **Disable maintenance mode.**
    ```bash
    fly ssh console --app $APP -C "rm /data/MAINTENANCE"
    fly machine restart <machine-id> --app $APP
    ```
    The logs must show `app_started poller_enabled=True`.
11. **Authenticated browser acceptance.** Sign in to the production frontend, then confirm:
    - the application count and dashboard figures match the step 2 aggregates;
    - the release's new screens or endpoints load;
    - the browser console and network show no errors.

    Never use a destructive action to test (for example, cancel a merge preview).
12. **Monitor and keep rollback references.**
    - Watch `fly logs` through at least two poll cycles for authentication, migration,
      polling, resolver and database errors.
    - Record the release ID, the merge commit, the step 2 backup, the pre-migration backup
      and the snapshot ID.
    - Keep them until the release is accepted.

### Rollback boundary

A migrated database and the application image are a pair:

- **Never restore a pre-migration backup under the new image.** In production the new
  image refuses to serve an outdated schema, so it would start in maintenance mode.
- **Never run the new schema under an old image without a stamp.** An old image refuses an
  unknown revision.

| Situation | Rollback |
|---|---|
| Before step 5 (nothing deployed) | Remove the flag and restart. Nothing changed. |
| Migration failed or verification failed (step 7–8) | Stay in maintenance mode. Restore the pre-migration backup **and** redeploy the previous image (`fly releases --app $APP`, then `fly deploy --app $APP --image <previous image>`). Then remove the flag. |
| Released, problem found, data written since | Prefer rolling forward. If the release must go, choose between: (a) restore a backup and redeploy the previous image, accepting the loss of writes made after the backup; or (b) roll back code only with `migrate_database.py stamp <old revision>` (expand-only schemas), keeping the data. |
| Whole volume lost | Restore from the step 3 snapshot (see the restore runbook). Then match the image to the schema the snapshot holds. |

Restoring the database without redeploying the matching image, or the reverse, leaves the
app in maintenance mode or running against a schema it doesn't know.

## Rollback

Every Phase 1 migration is additive. Choose the smallest step that fixes the problem.

- **Migration command failed.** The database is unchanged if the failure happened before
  `Migrated:`. If it happened after, restore the pre-migration backup. The command prints the
  exact restore command:
  ```bash
  fly ssh console --app $APP -C "python /app/scripts/restore_database.py /data/backups/<ts>-pre-migration --destination /data/applications.db --force"
  ```
  Then remove the maintenance flag and restart the machine.
- **Migration succeeded but the new release misbehaves.** Two options:
  - **Roll forward:** deploy a fix.
  - **Roll back both code and data:** with the app in maintenance mode, restore the
    pre-migration backup as above, then deploy the previous image
    (`fly releases --app $APP` → `fly deploy --app $APP --image <previous image>`), remove
    the flag and restart.

  - **Roll back code only, keep data** (expand-only revisions such as `0002`): an older
    release refuses a revision it does not know and starts in maintenance mode. Record the
    older revision first — the schema itself is not changed, and a verified backup is taken:
    ```bash
    fly ssh console --app $APP -C "touch /data/MAINTENANCE"   # then restart the machine
    fly ssh console --app $APP -C "python /app/scripts/migrate_database.py stamp 0001_baseline"
    ```
    Then deploy the previous image, remove the flag and restart. `stamp` only moves the
    recorded revision down. When the newer release returns, `migrate_database.py upgrade`
    re-applies the idempotent revision and runtime maintenance refreshes derived columns.

  A release that doesn't check revisions at all (pre-Phase-1) needs no stamp.
- **Data loss or corruption found later.** Follow the restore runbook below with the newest
  backup taken before the problem. Anything written after that backup is lost. Gmail-derived
  data comes back on the next polls; manual edits do not.

The files `--force` replaced stay beside the database as
`applications.db.pre-restore-<timestamp>`. Delete them by hand once you are satisfied.

## Fly.io backup runbook

**Where backups live.** `/data/backups` on the same volume as the database. Two extra
layers cover losing the volume itself:
- Fly volume snapshots (daily, kept 5 days by default). Check them with
  `fly volumes snapshots list <volume-id>`.
- Periodic off-provider copies (below).

**Scheduled backups.** `.github/workflows/fly-backup.yml` runs daily at 03:17 UTC and on
demand. It creates a verified backup over `fly ssh`, keeps 14, and re-verifies the newest.
It is off until you set the repository variable `FLY_BACKUPS_ENABLED=true`. It uses the
existing `FLY_API_TOKEN` secret, and nothing is downloaded into GitHub.

**Manual backup.** Safe while the app runs:
```bash
fly ssh console --app $APP -C "python /app/scripts/backup_database.py --label manual"
fly ssh console --app $APP -C "python /app/scripts/verify_backup.py --latest"
```

**Off-provider copy.** Pull a verified backup directory to a machine you control, and keep
it encrypted (the files contain personal data):
```bash
fly ssh console --app $APP -C "ls /data/backups"
fly ssh sftp get /data/backups/<name>/applications.db ./<name>/applications.db --app $APP
fly ssh sftp get /data/backups/<name>/manifest.json   ./<name>/manifest.json   --app $APP
python scripts/verify_backup.py ./<name>                        # locally
```

**Disk space.** Each backup is about the size of the database. Check usage with
`fly ssh console --app $APP -C "df -h /data"`.

## Fly.io restore runbook

1. Quiesce: `touch /data/MAINTENANCE`, then restart the machine (see step 3 above).
2. Choose and verify a backup:
   ```bash
   fly ssh console --app $APP -C "ls /data/backups"
   fly ssh console --app $APP -C "python /app/scripts/verify_backup.py /data/backups/<name>"
   ```
   To restore an off-provider copy, first upload both files into a new directory with
   `fly ssh sftp shell` (`mkdir /data/backups/<name>`, then `put`), and verify it.
3. Restore. The current database and its `-wal`/`-shm` files are moved aside, not deleted:
   ```bash
   fly ssh console --app $APP -C "python /app/scripts/restore_database.py /data/backups/<name> --destination /data/applications.db --force"
   ```
4. If the backup predates the current schema, `migrate_database.py status` reports
   `outdated` or `unversioned`. Run `migrate_database.py upgrade` while still in maintenance
   mode.
5. Run diagnostics, remove `/data/MAINTENANCE`, and restart the machine.

**Restore from a volume snapshot** (the whole volume was lost or damaged): run
`fly volumes create --snapshot-id <id> --region sin job_tracker_data_sin --app $APP`. Then
attach the new volume to the machine, or recreate the machine with it, and start in
maintenance mode. Run `migrate_database.py status` and the diagnostics before removing the
flag.

## Local development

- Backups default to `.job-tracker/backups/`. When an older local database is
  auto-upgraded, the backup taken first is labelled `auto-pre-migration`.
- To practise the production flow locally, set `DB_AUTO_MIGRATE=false`, start the API (it
  enters maintenance mode), stop it, then run
  `python scripts/migrate_database.py upgrade --allow-live`.

## Operational risks

| Risk | Mitigation |
|---|---|
| Backups share the volume with the database | Volume snapshots and periodic off-provider copies; the restore runbook covers snapshots |
| A migration fails partway (SQLite DDL is not fully transactional through pysqlite) | Mandatory verified pre-migration backup, a trial migration on a copy, and an integrity check plus diagnostics after migrating, with the exact restore command printed |
| Downtime during maintenance mode; Gmail polling pauses | Short procedure; the poller catches up from the stored history ID. Gmail history IDs expire after about a week, after which a backfill is needed |
| Restoring loses writes made after the backup | Back up immediately before risky operations; Gmail-derived data re-polls; manual edits do not |
| Backups contain personal data | Directories 0700 and files 0600; never committed or put in CI artifacts; off-provider copies must be encrypted |
| Running out of disk on the volume | `--keep 14` retention; check `df -h /data` |
| Someone copies the live database file directly | This runbook, and the tools refuse unsafe overwrites |
| A newer release migrated the database and the code is then rolled back | Unknown revisions force maintenance mode rather than risking incompatible writes |
