"""Alembic environment for the Job Tracker SQLite database.

Two ways in:
- Programmatic (backend.db.schema): the caller passes an open SQLAlchemy connection in
  ``config.attributes["connection"]`` so migrations run on the same engine (and pragmas)
  as DataStore.
- CLI (``alembic -c alembic.ini ...``): the URL comes from ``-x db=<path>``, then
  ``sqlalchemy.url`` if set, then backend.config.DB_PATH.

``render_as_batch=True`` makes ALTER operations SQLite-safe (copy-and-move when needed).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool
from sqlmodel import SQLModel

import backend.db.models  # noqa: F401  — registers every table on SQLModel.metadata
from backend.config import DB_PATH

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Used only by `alembic revision --autogenerate`; revisions themselves never import models.
target_metadata = SQLModel.metadata


def _database_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("db"):
        return f"sqlite:///{x_args['db']}"
    return config.get_main_option("sqlalchemy.url") or f"sqlite:///{DB_PATH}"


def _configure_and_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
        return
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _configure_and_run(conn)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
