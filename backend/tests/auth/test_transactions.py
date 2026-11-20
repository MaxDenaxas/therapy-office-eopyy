"""Request-scoped database sessions: commit before the response, rollback on any error,
read-only GET transactions, and SQL errors without bound values."""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import APIRouter
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.deps import DbSession
from app.db import make_engine, read_only_engine
from app.errors import ApiError
from app.models import AuditLog
from tests.local_http import ORIGIN, local_client


def audit_count(db) -> int:
    return db(lambda s: s.execute(select(func.count()).select_from(AuditLog)).scalar_one())


@pytest.fixture()
def tx_client(app):
    """Test-only routes (not part of the real application)."""
    router = APIRouter(prefix="/api/test")

    @router.post("/write")
    def write(db: DbSession):
        db.add(AuditLog(action="test.write", entity="test"))
        return {"ok": True}

    @router.post("/fail-at-commit")
    def fail_at_commit(db: DbSession):
        db.add(AuditLog(action="   ", entity="test"))  # violates a CHECK; flushed at commit
        return {"ok": True}

    @router.post("/write-then-refuse")
    def write_then_refuse(db: DbSession):
        db.add(AuditLog(action="test.write", entity="test"))
        db.flush()
        raise ApiError(409, "conflict")

    @router.get("/read")
    def read(db: DbSession):
        return {"n": db.execute(select(func.count()).select_from(AuditLog)).scalar_one()}

    @router.get("/write-in-get")
    def write_in_get(db: DbSession):
        db.add(AuditLog(action="test.write", entity="test"))
        db.flush()
        return {"ok": True}

    app.include_router(router)
    with local_client(app, raise_server_exceptions=False) as c:
        yield c


def test_successful_write_is_committed(tx_client, db):
    assert tx_client.post("/api/test/write", headers=ORIGIN).status_code == 200
    assert audit_count(db) == 1


def test_a_failed_commit_is_a_500_not_a_silent_success(tx_client, db, caplog):
    r = tx_client.post("/api/test/fail-at-commit", headers=ORIGIN)
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "internal_error" and len(body["request_id"]) == 16
    assert audit_count(db) == 0
    assert f"unhandled IntegrityError request_id={body['request_id']}" in caplog.text
    assert "action" not in caplog.text and "CHECK" not in caplog.text


def test_an_error_response_rolls_back_the_request(tx_client, db):
    r = tx_client.post("/api/test/write-then-refuse", headers=ORIGIN)
    assert r.status_code == 409 and r.json() == {"code": "conflict"}
    assert audit_count(db) == 0


def test_get_requests_are_read_only(tx_client, db):
    assert tx_client.get("/api/test/read").json() == {"n": 0}
    assert tx_client.get("/api/test/write-in-get").status_code == 500
    assert audit_count(db) == 0


def test_get_requests_do_not_wait_for_the_write_lock(tx_client, data_dir):
    holder = sqlite3.connect(data_dir / "therapy.db", isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock
        assert tx_client.get("/api/test/read").status_code == 200
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_read_only_and_write_transactions_share_the_pool_safely(data_dir):
    engine = make_engine(data_dir / "therapy.db")
    try:
        reader = read_only_engine(engine)
        with reader.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar() == 1
            with pytest.raises(Exception, match="readonly"):
                conn.execute(text("INSERT INTO audit_log (action, entity) VALUES ('a', 'b')"))
        with engine.begin() as conn:  # same pooled connection, write mode again
            assert conn.exec_driver_sql("PRAGMA query_only").scalar() == 0
            conn.execute(text("INSERT INTO audit_log (action, entity) VALUES ('a', 'b')"))
    finally:
        engine.dispose()


def test_sql_errors_never_contain_bound_values(data_dir):
    engine = make_engine(data_dir / "therapy.db")
    secret = "11111111111"
    try:
        with pytest.raises(IntegrityError) as info, engine.begin() as conn:
            for _ in range(2):
                conn.execute(
                    text(
                        "INSERT INTO patients (first_name, last_name, father_name, amka) "
                        "VALUES (:f, :l, :p, :a)"
                    ),
                    {"f": "Όνομα", "l": "Επώνυμο", "p": "Πατρώνυμο", "a": secret},
                )
        message = str(info.value)
        assert "UNIQUE constraint failed: patients.amka" in message
        assert secret not in message and "Επώνυμο" not in message
    finally:
        engine.dispose()


# ---------------------------------------------------------------- best-effort writes
def test_busy_timeout_is_restored_after_a_failed_best_effort_write(data_dir):
    """A failed fast-fail write must not leave its short lock wait on the pooled connection."""
    from app.db import DEFAULT_BUSY_TIMEOUT_MS, best_effort_engine

    engine = make_engine(data_dir / "therapy.db")
    try:
        fast = best_effort_engine(engine, wait_ms=50)
        holder = sqlite3.connect(data_dir / "therapy.db", isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with pytest.raises(OperationalError, match="locked"):
                with fast.begin() as conn:
                    conn.execute(text("SELECT 1"))
            assert time.monotonic() - started < 2  # it gave up quickly, not after 5 s
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        # the very same pooled connection, now through the normal engine
        assert engine.pool.checkedout() == 0
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == DEFAULT_BUSY_TIMEOUT_MS
            assert DEFAULT_BUSY_TIMEOUT_MS == 5000
        # and through a successful best-effort write, which restores it too
        with fast.begin() as conn:
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    finally:
        engine.dispose()


def test_the_application_best_effort_path_restores_the_timeout_too(
    app, client, logged_in, data_dir, clock
):
    """Through the real request path: a skipped touch, then a normal write transaction."""
    from tests.auth.test_sessions import write_lock_held

    database = client.app.state.database
    clock.advance(minutes=5)
    with write_lock_held(data_dir):
        assert client.get("/api/auth/session").json()["authenticated"] is True  # touch skipped
    with database.transaction() as db:
        assert db.connection().exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
