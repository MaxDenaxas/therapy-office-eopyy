"""Fail closed: no database, an outdated database, or no administrator → no access."""

from __future__ import annotations

import sqlite3

from app import migrate
from app.auth import service
from app.auth.dependencies import SESSION_COOKIE
from app.config import Settings
from app.main import create_app
from tests.auth.conftest import login, unsafe_headers
from tests.local_http import ORIGIN, local_client


def test_missing_database_answers_503_and_is_never_created(tmp_path):
    data = tmp_path / "empty"
    data.mkdir()
    app = create_app(Settings(data_dir=data))
    token = service.new_session_token()
    with local_client(app, cookies={SESSION_COOKIE: token}) as c:
        assert c.get("/api/health").status_code == 200  # liveness needs no database
        assert c.get("/api/auth/session").status_code == 503
        r = c.post("/api/auth/logout", headers=unsafe_headers(service.csrf_token_for(token)))
        assert r.status_code == 503 and r.json() == {"code": "service_unavailable"}
        r = c.post("/api/auth/login", json={"username": "a" * 3, "password": "b"}, headers=ORIGIN)
        assert r.status_code == 503
    assert not (data / "therapy.db").exists()


def test_without_a_cookie_protected_routes_are_401_even_without_a_database(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with local_client(app) as c:
        assert c.post("/api/auth/logout", headers=unsafe_headers("x" * 43)).status_code == 401


def test_outdated_schema_answers_503(app, data_dir):
    migrate.downgrade(data_dir / "therapy.db", "0002")
    with local_client(app) as c:
        r = c.post("/api/auth/login", json={"username": "abc", "password": "x"}, headers=ORIGIN)
        assert r.status_code == 503


def test_database_becomes_usable_once_initialised(tmp_path):
    data = tmp_path / "later"
    data.mkdir()
    app = create_app(Settings(data_dir=data))
    with local_client(app, cookies={SESSION_COOKIE: service.new_session_token()}) as c:
        assert c.get("/api/auth/session").status_code == 503
        migrate.upgrade(data / "therapy.db")
        assert c.get("/api/auth/session").json() == {"authenticated": False}


def test_without_an_administrator_nothing_is_accessible(client):
    for password in ["", "admin", "password", "a long test passphrase"]:
        r = login(client, username="admin", password=password or "x")
        assert r.status_code == 401
    assert client.get("/api/auth/session").json() == {"authenticated": False}
    assert client.post("/api/auth/logout", headers=unsafe_headers("x" * 43)).status_code == 401


def test_session_of_a_removed_account_is_refused(client, logged_in):
    """Defensive: if the account row ever disappeared, its sessions die with it.
    (The foreign key forbids this; it is switched off on a raw connection to force it.)"""
    con = sqlite3.connect(client.app.state.database.path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("DELETE FROM admin_account")
        con.commit()
    finally:
        con.close()
    assert client.get("/api/auth/session").json() == {"authenticated": False}
    assert client.post("/api/auth/logout", headers=unsafe_headers(logged_in)).status_code == 401
