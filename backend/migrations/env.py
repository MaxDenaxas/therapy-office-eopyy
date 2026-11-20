"""Alembic environment.

The database path is taken from ``config.attributes["db_path"]`` (set by
``app.migrate``) or, when run as plain ``alembic``, from the application
settings (THERAPY_DATA_DIR). Connections come from ``app.db.make_engine`` so
migrations run with the same pragmas and BEGIN IMMEDIATE as the application.

Foreign-key enforcement is switched off while a migration runs (batch mode
recreates tables, which would otherwise trip RESTRICT rules) and every
migration is followed by ``PRAGMA foreign_key_check``: any orphan aborts.
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import Connection

from app.db import database_url, make_engine
from app.models import Base

config = context.config
if config.config_file_name and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _db_path() -> Path:
    path = config.attributes.get("db_path")
    if path is not None:
        return Path(path)
    from app.config import get_settings

    return get_settings().database_path


def _set_foreign_keys(connection: Connection, on: bool) -> None:
    # must run outside a transaction, so go to the driver connection directly
    connection.connection.driver_connection.execute(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")


def _run(connection: Connection) -> None:
    _set_foreign_keys(connection, on=False)
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite cannot ALTER most things in place
            compare_type=True,
            transactional_ddl=True,  # SQLite DDL is transactional: a failed migration rolls back
        )
        with context.begin_transaction():
            context.run_migrations()
        orphans = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        connection.commit()
        if orphans:
            raise RuntimeError(f"Migration left rows violating foreign keys: {orphans[:5]}")
    finally:
        _set_foreign_keys(connection, on=True)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(_db_path()),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = make_engine(_db_path())
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
