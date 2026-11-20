"""Progressive login backoff on the single administrator account."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.auth import service
from app.models import AdminAccount, AuditLog
from tests.auth.conftest import login

WRONG = "not the right password"


def fail(client, times: int, username=None):
    for _ in range(times):
        kwargs = {"password": WRONG}
        if username:
            kwargs["username"] = username
        assert login(client, **kwargs).status_code == 401


@pytest.mark.parametrize(
    "failures, seconds",
    [
        (0, 0),
        (1, 0),
        (4, 0),
        (5, 30),
        (6, 60),
        (7, 120),
        (8, 240),
        (9, 480),
        (10, 900),
        (11, 900),
        (50, 900),
        (10**6, 900),
    ],
)
def test_lock_schedule(failures, seconds):
    assert service.lock_seconds(failures) == seconds


def test_four_failures_do_not_lock(client, admin):
    fail(client, 4)
    assert login(client).status_code == 200


def test_fifth_failure_locks_even_the_right_password(client, admin, clock, db):
    fail(client, 5)
    r = login(client)  # correct password, but locked
    assert r.status_code == 429
    assert r.json() == {"code": "login_locked", "retry_after": 30}
    assert r.headers["retry-after"] == "30"
    assert "set-cookie" not in r.headers
    clock.advance(seconds=29)
    assert login(client).json()["retry_after"] == 1
    clock.advance(seconds=1)
    assert login(client).status_code == 200
    acc = db(lambda s: s.get(AdminAccount, 1))
    assert acc.failed_login_count == 0 and acc.locked_until is None


def test_attempts_while_locked_are_not_counted_or_checked(client, admin, clock, db):
    fail(client, 5)
    for _ in range(10):
        assert login(client, password=WRONG).status_code == 429
    assert db(lambda s: s.get(AdminAccount, 1)).failed_login_count == 5


def test_delay_doubles_and_is_capped(client, admin, clock):
    fail(client, 5)
    expected = [60, 120, 240, 480, 900, 900]
    for seconds in expected:
        clock.advance(seconds=service.LOCK_MAX_SECONDS)
        assert login(client, password=WRONG).status_code == 401
        assert login(client).json()["retry_after"] == seconds


def test_unknown_usernames_count_against_the_account(client, admin):
    fail(client, 5, username="somebody-else")
    assert login(client).status_code == 429  # same lock: nothing to learn from the username


def test_lockouts_are_audited_without_personal_data(client, admin, db, clock):
    fail(client, 5)
    clock.advance(seconds=30)
    fail(client, 1)
    entries = db(
        lambda s: s.execute(
            select(AuditLog.action, AuditLog.details).where(AuditLog.action == "auth.lockout")
        ).all()
    )
    assert [json.loads(d) for _, d in entries] == [
        {"failed_login_count": 5, "locked_seconds": 30},
        {"failed_login_count": 6, "locked_seconds": 60},
    ]
