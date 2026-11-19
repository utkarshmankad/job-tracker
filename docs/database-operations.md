# Database operations: migrations, backups, restores

Job Tracker stores everything in one SQLite file: `applications.db` in `JOB_TRACKER_DIR`.
By default that is `.job-tracker/applications.db`.
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
- `/api/v1/health` still returns `{"status":"ok"}`, so health checks keep passing.

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
- `0005_source_collection` (Phase 3) adds the collector, enrollment, source, run, batch,
  source-item and source-observation tables — additive and idempotent; its downgrade
  refuses while any source observation exists. Details:
  [`phase-3-source-collection.md`](phase-3-source-collection.md) §2.
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

## Backups, migrations and restores (local)

The database, its backups and the maintenance flag all live under `JOB_TRACKER_DIR`
(`.job-tracker/` by default). Nothing is stored off this machine.

**Backup.** Safe while the app runs; each backup is verified and the oldest are pruned:

```bash
python scripts/backup_database.py --label manual
python scripts/verify_backup.py --latest
```

Backups land in `.job-tracker/backups/`, one directory each (`applications.db` plus
`manifest.json`). Take one immediately before any risky operation. There is no scheduled or
remote backup; run this yourself (or from `launchd`) as often as you need.

**Migrate** (when `DB_AUTO_MIGRATE=false` or the API reports maintenance mode):

1. `python scripts/backup_database.py --label pre-migration` and verify it.
2. Stop the API, then `python scripts/migrate_database.py upgrade --allow-live`.
3. `python scripts/migrate_database.py status`, then run `python -m backend.diagnostics`.
4. Remove `.job-tracker/MAINTENANCE` if present and start the API.

**Restore.** The current database and its `-wal`/`-shm` files are moved aside, not deleted:

```bash
python scripts/verify_backup.py .job-tracker/backups/<name>
python scripts/restore_database.py .job-tracker/backups/<name> --destination .job-tracker/applications.db --force
```

If the backup predates the current schema, `migrate_database.py status` reports `outdated`
or `unversioned`; run `migrate_database.py upgrade`. To roll the schema back without a
restore, `stamp` an older revision (see Tools; backup first). Then run the diagnostics and
restart the API.

The files `--force` replaced stay beside the database as
`applications.db.pre-restore-<timestamp>`. Delete them by hand once you are satisfied.

**Disk space.** Each backup is about the size of the database; `--keep 14` bounds the total.

## Local development

- Backups default to `.job-tracker/backups/`. When an older local database is
  auto-upgraded, the backup taken first is labelled `auto-pre-migration`.
- To practise the maintenance flow, set `DB_AUTO_MIGRATE=false`, start the API (it
  enters maintenance mode), stop it, then run
  `python scripts/migrate_database.py upgrade --allow-live`.

## Operational risks

| Risk | Mitigation |
|---|---|
| Backups share the disk with the database | Copy `.job-tracker/backups/` to an encrypted external drive periodically; a lost disk loses both |
| A migration fails partway (SQLite DDL is not fully transactional through pysqlite) | Mandatory verified pre-migration backup, a trial migration on a copy, and an integrity check plus diagnostics after migrating, with the exact restore command printed |
| Downtime during maintenance mode; Gmail polling pauses | Short procedure; the poller catches up from the stored history ID. Gmail history IDs expire after about a week, after which a backfill is needed |
| Restoring loses writes made after the backup | Back up immediately before risky operations; Gmail-derived data re-polls; manual edits do not |
| Backups contain personal data | Directories 0700 and files 0600; never committed; keep any external copy encrypted |
| Running out of disk | `--keep 14` retention; check `df -h` |
| Someone copies the live database file directly | This runbook, and the tools refuse unsafe overwrites |
| A newer release migrated the database and the code is then rolled back | Unknown revisions force maintenance mode rather than risking incompatible writes |
