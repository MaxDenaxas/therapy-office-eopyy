"""SQLite engine and session setup.

Every connection gets the same pragmas:
  * foreign_keys = ON   SQLite ignores foreign keys unless this is set per connection
  * journal_mode = WAL  readers never block the writer; crash-safe
  * synchronous = FULL  durability over speed (health records, low write volume)
  * busy_timeout        wait instead of failing immediately when the file is locked

Transactions: pysqlite's implicit BEGIN is switched off and every SQLAlchemy
transaction starts with ``BEGIN IMMEDIATE``. That takes the write lock up front,
so two requests cannot both read and then fail when upgrading to a write.

Read-only work (``read_only_engine(engine)``, used for GET requests) starts with a
plain deferred ``BEGIN`` and ``PRAGMA query_only=ON``: it never takes the write
lock and SQLite itself refuses any write attempted inside it.

``hide_parameters=True``: SQL errors never carry bound values (AMKA, names,
password hashes) into exception messages or logs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_BUSY_TIMEOUT_MS = 5000
PRAGMAS: tuple[tuple[str, str], ...] = (
    ("foreign_keys", "ON"),
    ("journal_mode", "WAL"),
    ("synchronous", "FULL"),
    ("busy_timeout", str(DEFAULT_BUSY_TIMEOUT_MS)),
)


READ_ONLY_OPTION = "therapy_read_only"
BUSY_TIMEOUT_OPTION = "therapy_busy_timeout_ms"  # shorter lock wait for best-effort writes


def database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{Path(path).resolve().as_posix()}"


def make_engine(path: Path) -> Engine:
    engine = create_engine(database_url(path), hide_parameters=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None  # we issue BEGIN ourselves (see below)
        cursor = dbapi_connection.cursor()
        for name, value in PRAGMAS:
            cursor.execute(f"PRAGMA {name}={value}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection) -> None:
        if connection.get_execution_options().get(READ_ONLY_OPTION):
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
        else:
            connection.exec_driver_sql("PRAGMA query_only=OFF")
            wait = connection.get_execution_options().get(BUSY_TIMEOUT_OPTION)
            if wait is None:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                return
            connection.exec_driver_sql(f"PRAGMA busy_timeout={int(wait)}")
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            finally:  # the pooled connection goes back to the normal wait
                connection.exec_driver_sql(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")

    return engine


def read_only_engine(engine: Engine) -> Engine:
    """The same pool, but every transaction is deferred and ``query_only``."""
    return engine.execution_options(**{READ_ONLY_OPTION: True})


def best_effort_engine(engine: Engine, wait_ms: int = 200) -> Engine:
    """Write transactions that give up quickly (``database is locked``) instead of waiting."""
    return engine.execution_options(**{BUSY_TIMEOUT_OPTION: wait_ms})


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
