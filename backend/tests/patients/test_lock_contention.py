"""The read-only POST search under SQLite write-lock contention (M5.2, decisions D and Y).

Another connection holds ``BEGIN IMMEDIATE`` (think: a long document issue in M7). The search
must still answer promptly, *including* when the administrator's ``last_seen_at`` is old enough
to trigger the optional refresh: that refresh is best-effort (200 ms lock wait, then skipped —
``app/auth/dependencies.py``, unchanged by M5.2), and every security check stays enforced.

A control route on the ordinary ``DbSession`` (a write transaction for POST) shows that the
harness really detects a route that waits for the lock.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.api.deps import DbSession
from app.auth.dependencies import protected_router
from app.auth.service import ABSOLUTE_LIFETIME, IDLE_TIMEOUT, TOUCH_INTERVAL
from tests.auth.test_sessions import last_seen, write_lock_held
from tests.local_http import local_client
from tests.patients.amka import synthetic_amka
from tests.patients.conftest import PATIENTS, unsafe_headers

SEARCH = f"{PATIENTS}/search"
PROMPT = 3.0  # seconds; the normal busy timeout is 5 s


@pytest.fixture()
def control_route(app):
    router = protected_router(prefix="/api/test-lock")

    @router.post("/write-session-read")
    def read_in_a_write_session(db: DbSession):
        return {"n": db.execute(text("SELECT count(*) FROM patients")).scalar()}

    app.include_router(router)


def timed(call):
    started = time.monotonic()
    response = call()
    return response, time.monotonic() - started


def test_search_answers_while_the_write_lock_is_held_and_last_seen_is_stale(
    client, logged_in, create, data_dir, clock, db
):
    pid = create(1)["id"]
    before = last_seen(db)
    clock.advance(minutes=5)  # the optional last_seen_at refresh is due ...
    assert timedelta(minutes=5) >= TOUCH_INTERVAL and timedelta(minutes=5) < IDLE_TIMEOUT
    headers = unsafe_headers(logged_in)
    with write_lock_held(data_dir):
        r, elapsed = timed(lambda: client.post(SEARCH, json={"query": "γεωργ"}, headers=headers))
        assert r.status_code == 200, r.text
        assert [x["id"] for x in r.json()["items"]] == [pid]
        assert elapsed < PROMPT

        r, elapsed = timed(
            lambda: client.post(SEARCH, json={"amka": synthetic_amka(1)}, headers=headers)
        )
        assert r.status_code == 200 and r.json()["total"] == 1 and elapsed < PROMPT
    assert last_seen(db) == before  # ... but was skipped under the lock, not queued
    # once the lock is gone the next request refreshes it
    assert client.post(SEARCH, json={"query": "γεωργ"}, headers=headers).status_code == 200
    assert last_seen(db) == before + timedelta(minutes=5)


def test_security_checks_stay_enforced_while_the_lock_is_held(
    app, client, logged_in, create, data_dir, clock
):
    create(1)
    clock.advance(minutes=5)
    body = {"query": "γεωργ"}
    with write_lock_held(data_dir):
        r, elapsed = timed(lambda: client.post(SEARCH, json=body, headers=unsafe_headers(None)))
        assert r.status_code == 403 and r.json() == {"code": "csrf_failed"} and elapsed < PROMPT
        r = client.post(SEARCH, json=body, headers={"X-CSRF-Token": logged_in})  # no Origin
        assert r.status_code == 403 and r.json() == {"code": "origin_rejected"}
        with local_client(app) as anonymous:
            r = anonymous.post(SEARCH, json=body, headers=unsafe_headers(logged_in))
            assert r.status_code == 401
        clock.advance(minutes=31)  # idle timeout passes while the lock is still held
        r, elapsed = timed(
            lambda: client.post(SEARCH, json=body, headers=unsafe_headers(logged_in))
        )
        assert r.status_code == 401 and r.json() == {"code": "not_authenticated"}
        assert elapsed < PROMPT


def test_the_absolute_lifetime_stays_enforced_while_the_lock_is_held(
    client, logged_in, create, data_dir, clock
):
    create(1)
    headers = unsafe_headers(logged_in)
    elapsed = timedelta(0)
    while elapsed < ABSOLUTE_LIFETIME - timedelta(minutes=20):  # keep the session active
        clock.advance(minutes=20)
        elapsed += timedelta(minutes=20)
        assert client.post(SEARCH, json={"query": "γεωργ"}, headers=headers).status_code == 200
    clock.advance(minutes=25)  # past the 12 h limit, but only 25 min after the last request
    with write_lock_held(data_dir):
        r = client.post(SEARCH, json={"query": "γεωργ"}, headers=headers)
    assert r.status_code == 401 and r.json() == {"code": "not_authenticated"}


def test_control_a_write_session_route_does_wait_for_the_lock(
    client, logged_in, control_route, data_dir, clock
):
    """Proves the harness: the same read through ``DbSession`` on POST takes BEGIN IMMEDIATE and
    therefore waits for the busy timeout and fails, where the search did not."""
    clock.advance(minutes=5)
    path = "/api/test-lock/write-session-read"
    with local_client(client.app, cookies=dict(client.cookies), raise_server_exceptions=False) as c:
        with write_lock_held(data_dir):
            r, elapsed = timed(lambda: c.post(path, json={}, headers=unsafe_headers(logged_in)))
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    assert elapsed >= 4.5  # it waited for the 5 s busy timeout
    assert client.post(path, json={}, headers=unsafe_headers(logged_in)).status_code == 200


def test_list_and_detail_answer_while_the_write_lock_is_held_and_last_seen_is_stale(
    client, logged_in, create, data_dir, clock, db
):
    pid = create(1)["id"]
    before = last_seen(db)
    clock.advance(minutes=5)  # the optional last_seen_at refresh is due
    with write_lock_held(data_dir):
        r, elapsed = timed(lambda: client.get(PATIENTS))
        assert r.status_code == 200 and r.json()["total"] == 1 and elapsed < PROMPT
        r, elapsed = timed(lambda: client.get(f"{PATIENTS}/{pid}"))
        assert r.status_code == 200 and r.json()["item"]["amka"] == synthetic_amka(1)
        assert elapsed < PROMPT
        r, elapsed = timed(lambda: client.get(f"{PATIENTS}?active=false"))
        assert r.status_code == 200 and r.json()["total"] == 0 and elapsed < PROMPT
    assert last_seen(db) == before  # skipped under the lock
    assert client.get(PATIENTS).status_code == 200
    assert last_seen(db) == before + timedelta(minutes=5)
