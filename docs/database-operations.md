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
| `python scripts/migrate_database.py upgrade` | Takes a pre-migration backup, verifies it (including a trial migration on a temporary copy), migrates, then runs an integrity check and diagnostics |
| `python scripts/backup_database.py [--label L] [--keep N]` | Creates a verified backup and optionally deletes the oldest ones so N remain |
| `python scripts/verify_backup.py <dir> \| --latest` | Restores into a temporary directory and opens the copy through DataStore |
| `python scripts/restore_database.py <dir> --destination <file> [--force]` | Validates the backup and restores it. Refuses to overwrite unless `--force` is given, which moves the existing files aside |
| `python -m backend.diagnostics` | Health report: schema revision, required tables, enum format, poller, Gmail credentials |

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
own terminal.

1. **Check the current revision.**
   ```bash
   fly ssh console --app $APP -C "python /app/scripts/migrate_database.py status"
   ```
   The first deploy of Phase 1 shows `unversioned`. After a deploy that adds a revision, the
   app is already in maintenance mode, because it refuses to start on an outdated schema.
2. **Take an extra safety net (optional, recommended).**
   ```bash
   fly volumes list --app $APP                  # note the volume ID
   fly volumes snapshots create <volume-id>
   ```
3. **Stop or quiesce writers.**
   ```bash
   fly ssh console --app $APP -C "touch /data/MAINTENANCE"
   fly machine restart <machine-id> --app $APP   # comes back in maintenance mode
   ```
4. **Create a verified backup and apply the migration.** These are one command: the upgrade
   refuses to proceed if the backup or its verification fails.
   ```bash
   fly ssh console --app $APP -C "python /app/scripts/migrate_database.py upgrade"
   ```
   The output lists the backup directory, the verification, `Integrity: ok`, and every
   diagnostic. Note the backup path for rollback.
5. **Run integrity and application diagnostics again (read-only).**
   ```bash
   fly ssh console --app $APP -C "sh -c 'cd /app && python -m backend.diagnostics'"
   ```
   `poller_state` may show stale while quiesced. That's expected.
6. **Resume service.**
   ```bash
   fly ssh console --app $APP -C "rm /data/MAINTENANCE"
   fly machine restart <machine-id> --app $APP
   ```
   Then sign in and check **Status**: Database Schema should show the head revision.

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

  Rolling back code alone is safe only to a release that doesn't check revisions (the
  pre-Phase-1 release). A Phase 1 release whose head is older than the database's revision
  starts in maintenance mode by design.
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
