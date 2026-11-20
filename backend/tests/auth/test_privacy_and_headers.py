"""Security headers on every response, privacy-safe errors and logs."""

from __future__ import annotations

import logging

import pytest

from app.auth.dependencies import SESSION_COOKIE
from app.config import Settings
from app.main import create_app
from tests.auth.conftest import PASSWORD, USERNAME, login, unsafe_headers
from tests.local_http import ORIGIN, local_client

COMMON = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
}
API = {
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


def has_headers(response, expected) -> bool:
    return all(response.headers.get(k) == v for k, v in expected.items())


def test_api_responses_carry_the_security_headers(client, admin):
    for r in [
        client.get("/api/health"),
        client.get("/api/auth/session"),
        login(client),
        login(client, password="wrong password!"),
        client.get("/api/does-not-exist"),
        client.post("/api/auth/login", json={}, headers=ORIGIN),
        client.post("/api/auth/login", json={}),  # refused by the origin check
        client.get("/api/health", headers={"Host": "evil.example"}),  # refused by host check
    ]:
        assert has_headers(r, COMMON | API), (r.request.url, dict(r.headers))


def test_unexpected_errors_carry_the_headers_and_nothing_else(app):
    @app.get("/api/boom")
    def boom():
        raise RuntimeError(f"secret detail {PASSWORD}")

    with local_client(app, raise_server_exceptions=False) as c:
        r = c.get("/api/boom")
    assert r.status_code == 500
    assert set(r.json()) == {"code", "request_id"}
    assert PASSWORD not in r.text and "secret" not in r.text
    assert has_headers(r, COMMON | API)


def test_frontend_gets_a_strict_content_security_policy(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>shell</body></html>", encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", frontend_dist=dist))
    with local_client(app) as c:
        r = c.get("/")
    assert r.status_code == 200 and has_headers(r, COMMON)
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_error_responses_never_issue_a_session(client, admin, clock):
    responses = [login(client, password="wrong password!") for _ in range(5)]
    responses += [
        login(client),  # locked: 429
        client.post("/api/auth/login", json={}, headers=ORIGIN),  # 422
        client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD}),  # 403
    ]
    for r in responses:
        assert r.status_code >= 400
        cookie = r.headers.get("set-cookie", "")
        assert cookie == "" or cookie.startswith('therapy_session=""'), cookie  # only a clear


def test_framework_errors_use_codes_only(client):
    assert client.get("/api/nope").json() == {"code": "not_found"}
    assert client.get("/api/auth/login").json() == {"code": "method_not_allowed"}


def test_validation_errors_list_location_and_message_only(client):
    r = client.post(
        "/api/auth/login",
        json={"username": 12345678901, "password": ["x" * 50], "extra": "ΜΥΣΤΙΚΟ"},
        headers=ORIGIN,
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    for err in body["errors"]:
        assert set(err) == {"loc", "msg"}
    assert "12345678901" not in r.text and "xxxxx" not in r.text and "ΜΥΣΤΙΚΟ" not in r.text


@pytest.mark.parametrize("level", [logging.DEBUG])
def test_logs_never_contain_credentials_or_tokens(client, admin, caplog, level):
    caplog.set_level(level)
    login(client, password="wrong password value")
    r = login(client)
    csrf = r.json()["csrf_token"]
    token = client.cookies[SESSION_COOKIE]
    client.get("/api/auth/session")
    client.post("/api/auth/logout", headers=unsafe_headers(csrf))
    text = caplog.text
    for secret in (PASSWORD, "wrong password value", token, csrf):
        assert secret not in text
    assert USERNAME not in text


def test_health_stays_minimal(client):
    assert set(client.get("/api/health").json()) == {"status", "version", "environment"}


def test_development_docs_are_not_blocked_by_the_api_policy(tmp_path):
    with local_client(create_app(Settings(data_dir=tmp_path))) as c:
        r = c.get("/api/docs")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert has_headers(r, COMMON)
