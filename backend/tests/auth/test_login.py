"""Login, logout and the session endpoint."""

from __future__ import annotations

from http.cookies import SimpleCookie

from argon2 import PasswordHasher, Type
from sqlalchemy import select, text

from app.auth import passwords, service
from app.auth.dependencies import SESSION_COOKIE
from app.models import AdminAccount, AdminSession, AuditLog
from tests.auth.conftest import PASSWORD, USERNAME, login, unsafe_headers
from tests.local_http import ORIGIN, local_client


def cookie_attributes(response) -> dict[str, str]:
    raw = response.headers["set-cookie"]
    parsed = SimpleCookie()
    parsed.load(raw)
    morsel = parsed[SESSION_COOKIE]
    return {k.lower(): v for k, v in morsel.items() if v} | {"_raw": raw}


def test_successful_login(client, admin, clock, db):
    r = login(client)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"csrf_token", "expires_at", "idle_timeout_seconds"}
    assert body["expires_at"] == "2026-01-15T21:00:00Z"  # 12 h after login
    assert body["idle_timeout_seconds"] == 1800
    acc = db(lambda s: s.get(AdminAccount, 1))
    assert acc.last_login_at == clock() and acc.failed_login_count == 0


def test_session_cookie_attributes(client, admin):
    attrs = cookie_attributes(login(client))
    raw = attrs["_raw"].lower()
    assert "httponly" in raw
    assert attrs["samesite"].lower() == "strict"
    assert attrs["path"] == "/"
    assert "domain" not in attrs
    assert attrs["max-age"] == str(12 * 3600)  # the absolute lifetime
    assert "secure" not in raw  # plain http on loopback


def test_cookie_is_secure_over_https(app, admin):
    with local_client(app, base_url="https://127.0.0.1:8443") as c:
        r = c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower()


def test_unknown_username_and_wrong_password_look_identical(client, admin):
    wrong_password = login(client, password="not the right password")
    unknown_user = login(client, username="somebody-else")
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json() == {"code": "invalid_credentials"}
    assert set(wrong_password.headers) == set(unknown_user.headers)


def test_username_is_case_sensitive(client, admin):
    assert login(client, username=USERNAME.upper()).status_code == 401


def test_login_without_any_administrator(client):
    r = login(client, username="admin", password="any password at all")
    assert r.status_code == 401 and r.json() == {"code": "invalid_credentials"}
    assert "set-cookie" not in r.headers or 'therapy_session=""' in r.headers["set-cookie"]


def test_login_body_validation_never_echoes_values(client, admin):
    secret = "p" * 2000
    r = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": secret}, headers=ORIGIN
    )
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"
    assert "ppppp" not in r.text
    r = client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD, "role": "superuser"},
        headers=ORIGIN,
    )
    assert r.status_code == 422 and PASSWORD not in r.text and "superuser" not in r.text
    r = client.post(
        "/api/auth/login",
        content=b"{not json",
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 422 and "not json" not in r.text


def test_login_replaces_a_presented_session(app, client, admin, db):
    login(client)
    first = client.cookies[SESSION_COOKIE]
    login(client)
    second = client.cookies[SESSION_COOKIE]
    assert first != second
    rows = db(lambda s: s.execute(select(AdminSession)).scalars().all())
    assert len(rows) == 1  # the first was revoked and cleaned up
    with local_client(app, cookies={SESSION_COOKIE: first}) as old:
        assert old.get("/api/auth/session").json() == {"authenticated": False}
    assert client.get("/api/auth/session").json()["authenticated"] is True


def test_login_upgrades_a_weak_hash(client, db):
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    db(lambda s: s.add(AdminAccount(id=1, username=USERNAME, password_hash=weak.hash(PASSWORD))))
    assert login(client).status_code == 200
    stored = db(lambda s: s.get(AdminAccount, 1)).password_hash
    assert not passwords.needs_rehash(stored)
    assert passwords.verify_password(stored, PASSWORD)


def test_session_endpoint(client, admin):
    assert client.get("/api/auth/session").json() == {"authenticated": False}
    csrf = login(client).json()["csrf_token"]
    info = client.get("/api/auth/session").json()
    assert info["authenticated"] is True
    assert info["csrf_token"] == csrf  # recomputed from the cookie, same token
    assert info["idle_timeout_seconds"] == 1800


def test_session_endpoint_clears_a_dead_cookie(client, admin):
    client.cookies.set(SESSION_COOKIE, "A" * 43)
    r = client.get("/api/auth/session")
    assert r.json() == {"authenticated": False}
    assert 'therapy_session=""' in r.headers["set-cookie"]


def test_logout(client, logged_in, db):
    r = client.post("/api/auth/logout", headers=unsafe_headers(logged_in))
    assert r.status_code == 204
    assert 'therapy_session=""' in r.headers["set-cookie"]
    assert db(lambda s: s.execute(select(AdminSession)).first()) is None
    assert client.get("/api/auth/session").json() == {"authenticated": False}
    # logging out again: no session any more
    assert client.post("/api/auth/logout", headers=unsafe_headers(logged_in)).status_code == 401


def test_logout_needs_the_session_and_csrf(client, logged_in):
    assert client.post("/api/auth/logout", headers=unsafe_headers()).status_code == 403
    assert client.post("/api/auth/logout", headers=unsafe_headers("x" * 43)).status_code == 403
    assert client.get("/api/auth/session").json()["authenticated"] is True


def test_audit_trail_has_ids_only(client, admin, db):
    login(client, password="not the right password")
    csrf = login(client).json()["csrf_token"]
    client.post("/api/auth/logout", headers=unsafe_headers(csrf))
    rows = db(lambda s: s.execute(select(AuditLog.action, AuditLog.details)).all())
    actions = [a for a, _ in rows]
    assert actions == ["admin.created", "auth.login", "auth.logout"]
    dump = repr(rows) + repr(db(lambda s: s.execute(text("SELECT * FROM audit_log")).all()))
    token = client.cookies.get(SESSION_COOKIE) or ""
    for secret in (USERNAME, PASSWORD, csrf):
        assert secret not in dump
    assert token == "" or token not in dump


def test_no_web_route_can_create_or_change_the_administrator(app):
    from fastapi.routing import iter_route_contexts

    for route in iter_route_contexts(app.routes):
        path = route.path or ""
        assert not any(word in path for word in ("setup", "register", "password", "signup"))


# ---------------------------------------------------------------- re-login with a session
def old_session_is_live(app, token) -> bool:
    with local_client(app, cookies={SESSION_COOKIE: token}) as c:
        return c.get("/api/auth/session").json()["authenticated"]


def test_failed_relogin_keeps_the_existing_session(app, client, admin, db):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    r = login(client, password="not the right password")
    assert r.status_code == 401
    assert "set-cookie" not in r.headers  # the browser keeps its cookie too
    assert client.cookies[SESSION_COOKIE] == token
    assert old_session_is_live(app, token)
    assert len(db(lambda s: s.execute(select(AdminSession)).scalars().all())) == 1


def test_unknown_username_relogin_keeps_the_existing_session(app, client, admin):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    assert login(client, username="somebody-else").status_code == 401
    assert old_session_is_live(app, token)


def test_locked_relogin_keeps_the_existing_session(app, client, admin, clock):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    for _ in range(5):
        assert login(client, password="not the right password").status_code == 401
    r = login(client)  # correct password, but locked
    assert r.status_code == 429 and "set-cookie" not in r.headers
    assert old_session_is_live(app, token)
    assert client.cookies[SESSION_COOKIE] == token


def test_validation_failure_keeps_the_existing_session(app, client, admin):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    assert client.post("/api/auth/login", json={}, headers=ORIGIN).status_code == 422
    assert old_session_is_live(app, token)


def test_successful_relogin_replaces_the_previous_session(app, client, admin, db):
    login(client)
    old = client.cookies[SESSION_COOKIE]
    r = login(client)
    new = client.cookies[SESSION_COOKIE]
    assert r.status_code == 200 and new != old
    assert not old_session_is_live(app, old)
    assert old_session_is_live(app, new)
    (row,) = db(lambda s: s.execute(select(AdminSession)).scalars().all())
    assert row.revoked_at is None  # exactly one session, the new one
    assert row.token_hash != service.sha256_hex(old)


def test_relogin_failure_after_the_old_session_expired_is_just_a_failure(app, client, admin, clock):
    login(client)
    clock.advance(minutes=31)
    assert login(client, password="not the right password").status_code == 401
    assert login(client).status_code == 200
