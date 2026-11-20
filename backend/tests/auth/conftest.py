"""Fixtures for authentication and API-foundation tests.

Each test gets its own data directory with a freshly migrated database (copied
from one migrated once per session), an application with a controllable clock
and a loopback client. No administrator exists unless the ``admin`` fixture is
used, and it is created through the same service the CLI uses.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta

import pytest

from app import migrate
from app.auth import service
from app.config import Settings
from app.db import make_engine, make_session_factory, session_scope
from app.main import create_app
from tests.local_http import ORIGIN, local_client

USERNAME = "office-admin"
PASSWORD = "a long test passphrase"  # synthetic; 22 characters
START = datetime(2026, 1, 15, 9, 0, 0)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture(scope="session")
def auth_db_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("auth-db-template") / "therapy.db"
    migrate.upgrade(path)
    return path


@pytest.fixture()
def data_dir(tmp_path, auth_db_template):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(auth_db_template, data / "therapy.db")
    return data


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def settings(data_dir):
    return Settings(data_dir=data_dir)


@pytest.fixture()
def app(settings, clock):
    return create_app(settings, clock=clock)


@pytest.fixture()
def client(app):
    with local_client(app) as c:
        yield c


@pytest.fixture()
def factory(data_dir):
    engine = make_engine(data_dir / "therapy.db")
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture()
def db(factory):
    """Run ``fn(session)`` in its own committed transaction: ``db(lambda s: ...)``."""

    def run(fn):
        with session_scope(factory) as s:
            return fn(s)

    return run


@pytest.fixture()
def admin(db, clock):
    db(lambda s: service.create_account(s, USERNAME, PASSWORD, clock()))
    return USERNAME, PASSWORD


def login(client, username=USERNAME, password=PASSWORD):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}, headers=ORIGIN
    )


@pytest.fixture()
def logged_in(client, admin):
    """A logged-in client; returns the CSRF token."""
    r = login(client)
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def unsafe_headers(csrf: str | None = None) -> dict[str, str]:
    headers = dict(ORIGIN)
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers
