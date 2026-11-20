"""Request-scoped database access.

The database is opened lazily and never created by the web application: if the
file is missing or its schema is not at the latest migration, every route that
needs it answers 503 (fail closed). ``python -m app.cli db init`` creates it.

Transactions
  * ``DbSession`` is one session per request. GET/HEAD get a read-only session
    (deferred BEGIN + ``query_only``); other methods get a write session
    (``BEGIN IMMEDIATE``).
  * The dependency uses ``scope="function"``: it commits right after the route
    function returns and *before* the response is sent, so a failed commit
    becomes a 500 instead of a silently lost write. Any exception, including an
    ``HTTPException``, rolls the whole request back.
  * Work that must persist even though the request fails (a failed login's
    counter) is returned as a normal response, never raised.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from alembic.runtime.migration import MigrationContext
from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import best_effort_engine, make_engine, make_session_factory, read_only_engine
from app.errors import ApiError

SAFE_METHODS = frozenset({"GET", "HEAD"})


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class DatabaseUnavailable(Exception):
    pass


class Database:
    """Lazy, thread-safe holder of the engine and the two session factories."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._engine: Engine | None = None
        self._write: sessionmaker[Session] | None = None
        self._read: sessionmaker[Session] | None = None
        self._touch: sessionmaker[Session] | None = None

    def _ensure_ready(self) -> None:
        if self._engine is not None:
            return
        with self._lock:
            if self._engine is not None:
                return
            if not self.path.is_file():  # never let SQLite create an empty database
                raise DatabaseUnavailable("database file not found")
            from app.migrate import head_revision

            engine = make_engine(self.path)
            try:
                with read_only_engine(engine).connect() as conn:  # never waits for writers
                    current = MigrationContext.configure(conn).get_current_revision()
            except Exception as exc:
                engine.dispose()
                raise DatabaseUnavailable("database cannot be opened") from exc
            if current != head_revision():
                engine.dispose()
                raise DatabaseUnavailable("database schema is not at the latest revision")
            self._write = make_session_factory(engine)
            self._read = make_session_factory(read_only_engine(engine))
            self._touch = make_session_factory(best_effort_engine(engine))
            self._engine = engine

    def factory(self, read_only: bool) -> sessionmaker[Session]:
        self._ensure_ready()
        factory = self._read if read_only else self._write
        assert factory is not None
        return factory

    @contextmanager
    def transaction(self, read_only: bool = False) -> Iterator[Session]:
        """A short unit of work outside the request session (used by authentication)."""
        session = self.factory(read_only)()
        try:
            yield session
            if read_only:
                session.rollback()
            else:
                session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def best_effort_write(self) -> Iterator[Session]:
        """A short write transaction that fails fast when the database is locked (the caller
        decides whether that matters). Used for the optional session-activity update."""
        self._ensure_ready()
        assert self._touch is not None
        session = self._touch()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
            self._engine = self._write = self._read = self._touch = None


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    try:
        database.factory(read_only=True)
    except DatabaseUnavailable as exc:
        raise ApiError(503, "service_unavailable") from exc
    return database


def get_clock(request: Request) -> Callable[[], datetime]:
    return request.app.state.clock


def get_db(request: Request) -> Iterator[Session]:
    database = get_database(request)
    read_only = request.method in SAFE_METHODS
    session = database.factory(read_only)()
    try:
        yield session
        if read_only:
            session.rollback()
        else:
            session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db, scope="function")]
