"""SQLite engine and session setup.

Every connection gets the same pragmas:
  * foreign_keys = ON   SQLite ignores foreign keys unless this is set per connection
  * journal_mode = WAL  readers never block the writer; crash-safe
  * synchronous = FULL  durability over speed (health records, low write volume)
  * busy_timeout        wait instead of failing immediately when the file is locked

Transactions: pysqlite's implicit BEGIN is switched off and every SQLAlchemy
transaction starts with ``BEGIN IMMEDIATE``. That takes the write lock up front,
so two requests cannot both read and then fail when upgrading to a write.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

PRAGMAS: tuple[tuple[str, str], ...] = (
    ("foreign_keys", "ON"),
    ("journal_mode", "WAL"),
    ("synchronous", "FULL"),
    ("busy_timeout", "5000"),
)


def database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{Path(path).resolve().as_posix()}"


def make_engine(path: Path) -> Engine:
    engine = create_engine(database_url(path))

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None  # we issue BEGIN ourselves (see below)
        cursor = dbapi_connection.cursor()
        for name, value in PRAGMAS:
            cursor.execute(f"PRAGMA {name}={value}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


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
