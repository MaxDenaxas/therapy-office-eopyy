"""Server-side sessions: hashing, idle and absolute expiry, touch, revocation, cleanup."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.deps import DbSession
from app.auth import service
from app.auth.dependencies import SESSION_COOKIE, protected_router
from app.models import AdminSession
from tests.auth.conftest import login, unsafe_headers
from tests.local_http import local_client


def rows(db):
    return db(lambda s: s.execute(select(AdminSession)).scalars().all())


def authenticated(client) -> bool:
    return client.get("/api/auth/session").json()["authenticated"]


def test_only_hashes_are_stored(client, logged_in, db, data_dir):
    token = client.cookies[SESSION_COOKIE]
    (row,) = rows(db)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.csrf_hash == hashlib.sha256(logged_in.encode()).hexdigest()
    raw = (data_dir / "therapy.db").read_bytes()
    wal = data_dir / "therapy.db-wal"
    raw += wal.read_bytes() if wal.exists() else b""
    assert token.encode() not in raw and logged_in.encode() not in raw


def test_tokens_are_random_and_well_formed():
    tokens = {service.new_session_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(service.well_formed(t) for t in tokens)
    t = service.new_session_token()
    assert service.well_formed(service.csrf_token_for(t))
    assert service.csrf_token_for(t) != t


def test_database_refuses_anything_but_a_hash(db, admin, clock):
    now = clock()
    with pytest.raises(IntegrityError, match="token_hash_sha256"):
        db(
            lambda s: s.add(
                AdminSession(
                    admin_id=1,
                    token_hash="plain-token",
                    csrf_hash="0" * 64,
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
        )


def test_session_identity_is_fixed_and_revocation_is_final(client, logged_in, db, clock):
    (row,) = rows(db)
    for sql in [
        "UPDATE admin_sessions SET token_hash = :h",
        "UPDATE admin_sessions SET csrf_hash = :h",
        "UPDATE admin_sessions SET expires_at = '2099-01-01 00:00:00'",
        "UPDATE admin_sessions SET created_at = '2020-01-01 00:00:00'",
    ]:
        with pytest.raises(IntegrityError, match="only be touched or revoked"):
            db(lambda s, sql=sql: s.execute(text(sql), {"h": "f" * 64}))
    db(lambda s: service.revoke_session(s, row.id, clock()))
    with pytest.raises(IntegrityError, match="only be touched or revoked"):
        db(lambda s: s.execute(text("UPDATE admin_sessions SET revoked_at = NULL")))
    assert not authenticated(client)


def test_idle_timeout(client, logged_in, clock):
    clock.advance(minutes=29, seconds=59)
    assert authenticated(client)  # activity: touches last_seen_at
    clock.advance(minutes=29, seconds=59)
    assert authenticated(client)  # 30 min idle counted from the last request
    clock.advance(minutes=30)
    assert not authenticated(client)


def test_idle_timeout_boundary_is_exclusive(client, logged_in, clock):
    clock.advance(minutes=30)
    assert not authenticated(client)


def test_absolute_lifetime_even_with_continuous_activity(client, logged_in, clock):
    for _ in range(12 * 4 - 1):  # a request every 15 minutes for 11 h 45 min
        clock.advance(minutes=15)
        assert authenticated(client)
    clock.advance(minutes=15)  # 12 h after login
    assert not authenticated(client)


def test_idle_and_absolute_expiry_are_checked_independently(db, admin, clock):
    """Directly on the service: a row that passes one check but not the other is refused."""
    now = clock()
    token = service.new_session_token()

    def add(created, last_seen, expires):
        def run(s):
            s.execute(text("DELETE FROM admin_sessions"))
            s.add(
                AdminSession(
                    admin_id=1,
                    token_hash=service.sha256_hex(token),
                    csrf_hash=service.sha256_hex(service.csrf_token_for(token)),
                    created_at=created,
                    last_seen_at=last_seen,
                    expires_at=expires,
                )
            )

        db(run)

    def valid():
        return db(lambda s: service.validate_session(s, token, now)) is not None

    hour = timedelta(hours=1)
    add(now - hour, now - timedelta(minutes=5), now + hour)
    assert valid()
    add(now - hour, now - timedelta(minutes=5), now)  # recently used, absolute limit reached
    assert not valid()
    add(now - hour, now - timedelta(minutes=31), now + 10 * hour)  # far from expiry, idle
    assert not valid()
    add(now + hour, now + hour, now + 2 * hour)  # created in the future: clock went back
    assert not valid()


def test_touch_is_rate_limited(client, logged_in, db, clock):
    start = rows(db)[0].last_seen_at
    clock.advance(seconds=30)
    authenticated(client)
    assert rows(db)[0].last_seen_at == start
    clock.advance(seconds=30)
    authenticated(client)
    assert rows(db)[0].last_seen_at == start + timedelta(seconds=60)


@pytest.mark.parametrize(
    "cookie",
    ["", "short", "A" * 42, "A" * 44, "A" * 42 + "=", "A" * 42 + ".", "A" * 42 + "+", "A" * 4000],
)
def test_malformed_cookies_are_refused(app, admin, cookie):
    with local_client(app, cookies={SESSION_COOKIE: cookie}) as c:
        assert c.get("/api/auth/session").json() == {"authenticated": False}
        assert c.post("/api/auth/logout", headers=unsafe_headers("A" * 43)).status_code == 401


def test_a_forged_well_formed_token_is_refused(app, admin):
    forged = service.new_session_token()
    with local_client(app, cookies={SESSION_COOKIE: forged}) as c:
        assert c.get("/api/auth/session").json() == {"authenticated": False}
        r = c.post("/api/auth/logout", headers=unsafe_headers(service.csrf_token_for(forged)))
        assert r.status_code == 401


def test_cleanup_removes_dead_sessions_only(db, admin, clock, app):
    for _ in range(3):
        with local_client(app) as c:
            assert login(c).status_code == 200
    clock.advance(minutes=20)
    with local_client(app) as live:
        login(live)
    clock.advance(minutes=15)  # the first three are idle now, the last is not
    removed = db(lambda s: service.cleanup_sessions(s, clock()))
    assert removed == 3
    assert len(rows(db)) == 1
    assert authenticated(live)


def test_at_most_ten_active_sessions(app, admin, db, clock):
    clients = []
    for _ in range(12):
        c = local_client(app)
        assert login(c).status_code == 200
        clients.append(c)
        clock.advance(seconds=1)
    assert db(lambda s: service.active_session_count(s, clock())) == 10
    alive = [authenticated(c) for c in clients]
    assert alive == [False, False] + [True] * 10  # the least recently used ones went
    for c in clients:
        c.close()


def test_revoked_sessions_stop_working_immediately(client, logged_in, db, clock):
    db(lambda s: service.revoke_all_sessions(s, clock()))
    assert not authenticated(client)
    assert client.post("/api/auth/logout", headers=unsafe_headers(logged_in)).status_code == 401


# ---------------------------------------------------------------- read-only validation
@contextmanager
def write_lock_held(data_dir):
    """Another connection (think: a long document issue) holds the SQLite write lock."""
    holder = sqlite3.connect(data_dir / "therapy.db", isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield
    finally:
        holder.execute("ROLLBACK")
        holder.close()


@pytest.fixture()
def protected_get(app):
    router = protected_router(prefix="/api/test")

    @router.get("/items")
    def items(db: DbSession):
        return {"ok": True}

    app.include_router(router)


def last_seen(db):
    return rows(db)[0].last_seen_at


def test_authenticated_get_does_not_fail_while_the_write_lock_is_held(
    client, logged_in, protected_get, data_dir, clock, db
):
    before = last_seen(db)
    clock.advance(minutes=5)  # the activity update is due
    with write_lock_held(data_dir):
        started = time.monotonic()
        assert client.get("/api/test/items").status_code == 200
        assert client.get("/api/auth/session").json()["authenticated"] is True
        assert time.monotonic() - started < 3  # did not wait for the 5 s busy timeout
    assert last_seen(db) == before  # the optional update was skipped, not queued
    assert client.get("/api/test/items").status_code == 200  # and happens on the next request
    assert last_seen(db) == before + timedelta(minutes=5)


def test_checks_still_apply_while_the_write_lock_is_held(
    client, logged_in, protected_get, data_dir, clock, db
):
    with write_lock_held(data_dir):
        assert client.get("/api/test/items").status_code == 200
        # idle expiry: refused although the activity update could not run either
        clock.advance(minutes=31)
        assert client.get("/api/test/items").status_code == 401
        assert client.get("/api/auth/session").json() == {"authenticated": False}


def test_revoked_and_unknown_sessions_are_refused_while_the_lock_is_held(
    app, client, logged_in, protected_get, data_dir, clock, db
):
    (row,) = rows(db)
    db(lambda s: service.revoke_session(s, row.id, clock()))
    with write_lock_held(data_dir):
        assert client.get("/api/test/items").status_code == 401
        with local_client(app, cookies={SESSION_COOKIE: service.new_session_token()}) as c:
            assert c.get("/api/test/items").status_code == 401


def test_csrf_is_still_enforced_while_the_write_lock_is_held(client, logged_in, data_dir, clock):
    clock.advance(minutes=5)
    with write_lock_held(data_dir):
        assert client.post("/api/auth/logout", headers=unsafe_headers()).status_code == 403
        assert client.post("/api/auth/logout", headers=unsafe_headers("x" * 43)).status_code == 403


def test_the_activity_update_is_not_attempted_when_not_due(client, logged_in, data_dir, db):
    with write_lock_held(data_dir):
        assert client.get("/api/auth/session").json()["authenticated"] is True
    assert last_seen(db) == rows(db)[0].created_at


def test_validation_alone_never_writes(db, admin, clock):
    now = clock()
    token = service.new_session_token()
    db(
        lambda s: s.add(
            AdminSession(
                admin_id=1,
                token_hash=service.sha256_hex(token),
                csrf_hash=service.sha256_hex(service.csrf_token_for(token)),
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=12),
            )
        )
    )
    later = now + timedelta(minutes=10)
    context, due = db(lambda s: service.check_session(s, token, later))
    assert due is True
    assert rows(db)[0].last_seen_at == now  # check_session left the row alone


def test_touch_never_undoes_a_revocation_or_moves_time_backwards(db, admin, clock):
    now = clock()
    token = service.new_session_token()
    db(
        lambda s: s.add(
            AdminSession(
                admin_id=1,
                token_hash=service.sha256_hex(token),
                csrf_hash=service.sha256_hex(service.csrf_token_for(token)),
                created_at=now,
                last_seen_at=now + timedelta(minutes=5),
                expires_at=now + timedelta(hours=12),
            )
        )
    )
    sid = rows(db)[0].id
    db(lambda s: service.touch_session(s, sid, now + timedelta(minutes=1)))  # older: ignored
    assert rows(db)[0].last_seen_at == now + timedelta(minutes=5)
    db(lambda s: service.revoke_session(s, sid, now + timedelta(minutes=6)))
    db(lambda s: service.touch_session(s, sid, now + timedelta(minutes=9)))  # revoked: ignored
    assert rows(db)[0].last_seen_at == now + timedelta(minutes=5)


def test_other_database_errors_in_the_activity_update_are_not_swallowed(
    app, client, logged_in, clock, monkeypatch
):
    def broken(db, session_id, now):
        raise OperationalError("UPDATE", None, Exception("disk I/O error"))

    monkeypatch.setattr(service, "touch_session", broken)
    clock.advance(minutes=5)
    with local_client(app, raise_server_exceptions=False, cookies=client.cookies) as c:
        assert c.get("/api/auth/session").status_code == 500


def test_session_validation_uses_a_read_only_transaction(app, client, logged_in, monkeypatch):
    seen = []
    original = service.check_session

    def spy(db, token, now):
        seen.append(db.connection().exec_driver_sql("PRAGMA query_only").scalar())
        return original(db, token, now)

    monkeypatch.setattr(service, "check_session", spy)
    assert client.get("/api/auth/session").json()["authenticated"] is True
    assert seen == [1]


# ---------------------------------------------------------------- session cap and re-login
def full_house(app, clock):
    """Ten clients, each with its own live session (the maximum)."""
    clients = []
    for _ in range(service.MAX_ACTIVE_SESSIONS):
        c = local_client(app)
        assert login(c).status_code == 200
        clients.append(c)
        clock.advance(seconds=1)
    return clients


def live(clients) -> list[bool]:
    return [authenticated(c) for c in clients]


def test_relogin_with_ten_live_sessions_replaces_only_the_presented_one(app, admin, db, clock):
    clients = full_house(app, clock)
    assert db(lambda s: service.active_session_count(s, clock())) == 10
    try:
        old_token = clients[4].cookies[SESSION_COOKIE]
        assert login(clients[4]).status_code == 200  # re-login from the 5th client
        assert clients[4].cookies[SESSION_COOKIE] != old_token
        assert db(lambda s: service.active_session_count(s, clock())) == 10  # still the maximum
        assert live(clients) == [True] * 10  # the 9 others untouched, the 5th has its new one
        with local_client(app, cookies={SESSION_COOKIE: old_token}) as stale:
            assert not authenticated(stale)  # the presented session is gone
    finally:
        for c in clients:
            c.close()


def test_a_new_client_logging_in_with_ten_live_sessions_evicts_only_the_least_recent(
    app, admin, db, clock
):
    clients = full_house(app, clock)
    try:
        with local_client(app) as newcomer:
            assert login(newcomer).status_code == 200
            assert authenticated(newcomer)
        assert db(lambda s: service.active_session_count(s, clock())) == 10
        assert live(clients) == [False] + [True] * 9
    finally:
        for c in clients:
            c.close()


def test_relogin_with_an_expired_presented_session_still_evicts_for_the_cap(app, admin, db, clock):
    clients = full_house(app, clock)
    try:
        with local_client(app, cookies={SESSION_COOKIE: "A" * 43}) as stranger:  # unknown token
            assert login(stranger).status_code == 200
        assert db(lambda s: service.active_session_count(s, clock())) == 10
        assert live(clients) == [False] + [True] * 9
    finally:
        for c in clients:
            c.close()


def test_failed_and_locked_relogin_keep_all_ten_sessions(app, admin, db, clock):
    clients = full_house(app, clock)
    try:
        for _ in range(5):
            assert login(clients[3], password="not the right password").status_code == 401
        assert login(clients[3]).status_code == 429  # locked
        assert db(lambda s: service.active_session_count(s, clock())) == 10
        assert live(clients) == [True] * 10
    finally:
        for c in clients:
            c.close()


def test_relogin_is_all_or_nothing(app, admin, db, clock, monkeypatch):
    """If creating the new session fails, the presented session is still alive."""
    clients = full_house(app, clock)
    try:
        old_token = clients[2].cookies[SESSION_COOKIE]

        def boom(db, now):
            raise RuntimeError("simulated failure while replacing the session")

        monkeypatch.setattr(service, "_enforce_session_cap", boom)
        with local_client(
            app, raise_server_exceptions=False, cookies={SESSION_COOKIE: old_token}
        ) as c:
            assert login(c).status_code == 500
            assert SESSION_COOKIE in c.cookies and c.cookies[SESSION_COOKIE] == old_token
        monkeypatch.undo()
        assert db(lambda s: service.active_session_count(s, clock())) == 10
        assert live(clients) == [True] * 10  # nothing was revoked: the transaction rolled back
    finally:
        for c in clients:
            c.close()
