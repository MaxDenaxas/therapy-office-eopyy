"""CSRF tokens and the request filter: client address, Host, Origin, content type."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.auth import service
from app.security import host_name, is_loopback_client
from tests.auth.conftest import PASSWORD, USERNAME, login, unsafe_headers
from tests.local_http import ORIGIN, local_client

CREDS = {"username": USERNAME, "password": PASSWORD}


# ---------------------------------------------------------------- CSRF
def test_csrf_token_is_required_for_unsafe_requests(client, logged_in):
    assert client.post("/api/auth/logout", headers=unsafe_headers()).json() == {
        "code": "csrf_failed"
    }
    assert client.post("/api/auth/logout", headers=unsafe_headers("")).status_code == 403
    truncated = unsafe_headers(logged_in[:-1])
    assert client.post("/api/auth/logout", headers=truncated).status_code == 403
    assert client.post("/api/auth/logout", headers=unsafe_headers(logged_in)).status_code == 204


def test_csrf_token_of_another_session_is_refused(app, admin):
    with local_client(app) as a, local_client(app) as b:
        csrf_a = login(a).json()["csrf_token"]
        csrf_b = login(b).json()["csrf_token"]
        assert csrf_a != csrf_b
        assert a.post("/api/auth/logout", headers=unsafe_headers(csrf_b)).status_code == 403
        assert a.post("/api/auth/logout", headers=unsafe_headers(csrf_a)).status_code == 204


def test_csrf_comparison_uses_hashes():
    token = service.new_session_token()
    csrf = service.csrf_token_for(token)
    stored = service.sha256_hex(csrf)
    assert service.csrf_matches(stored, csrf)
    assert not service.csrf_matches(stored, None)
    assert not service.csrf_matches(stored, csrf.swapcase())
    assert not service.csrf_matches(stored, stored)  # the hash itself is not the token


# ---------------------------------------------------------------- Origin
@pytest.mark.parametrize(
    "headers",
    [
        {},  # no Origin at all
        {"Origin": "null"},
        {"Origin": "http://evil.example"},
        {"Origin": "http://127.0.0.1:9999"},  # another local port is another origin
        {"Origin": "https://127.0.0.1:8000"},
        {"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-site"},
    ],
)
def test_unsafe_requests_need_the_same_origin(client, admin, headers):
    r = client.post("/api/auth/login", json=CREDS, headers=headers)
    assert r.status_code == 403 and r.json() == {"code": "origin_rejected"}


def test_same_origin_is_accepted(client, admin):
    headers = {**ORIGIN, "Sec-Fetch-Site": "same-origin"}
    assert client.post("/api/auth/login", json=CREDS, headers=headers).status_code == 200


def test_vite_dev_proxy_origin_is_same_origin(app, admin):
    """The Vite proxy keeps the browser's Host (changeOrigin: false), so the backend sees
    Origin http://127.0.0.1:5173 together with Host 127.0.0.1:5173 — one origin, no CORS."""
    with local_client(app, base_url="http://127.0.0.1:5173") as c:
        ok = c.post("/api/auth/login", json=CREDS, headers={"Origin": "http://127.0.0.1:5173"})
        assert ok.status_code == 200
        bad = c.post("/api/auth/login", json=CREDS, headers={"Origin": "http://localhost:5173"})
        assert bad.status_code == 403


def test_safe_requests_need_no_origin(client):
    assert client.get("/api/health").status_code == 200


def test_no_cors_is_ever_granted(client, admin):
    r = client.options(
        "/api/auth/login",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 403
    assert not any(h.lower().startswith("access-control-") for h in r.headers)
    r = client.post("/api/auth/login", json=CREDS, headers=ORIGIN)
    assert not any(h.lower().startswith("access-control-") for h in r.headers)


# ---------------------------------------------------------------- Host / client
@pytest.mark.parametrize(
    "base_url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"]
)
def test_loopback_host_names_are_accepted(app, base_url):
    with local_client(app, base_url=base_url) as c:
        assert c.get("/api/health").status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8000",
        "192.168.1.10:8000",
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "",
        "[::1",
        "[::1]x",
        "::1",
    ],
)
def test_other_host_headers_are_refused(client, host):
    """DNS rebinding: a foreign name resolving to 127.0.0.1 still carries its own Host."""
    r = client.get("/api/health", headers={"Host": host})
    assert r.status_code == 400 and r.json() == {"code": "invalid_host"}


@pytest.mark.parametrize("peer", ["192.168.1.20", "10.0.0.5", "8.8.8.8", "testclient", "::2"])
def test_non_loopback_clients_are_refused(app, peer):
    with TestClient(app, base_url="http://127.0.0.1:8000", client=(peer, 40000)) as c:
        assert c.get("/api/health").status_code == 403
        assert c.post("/api/auth/login", json=CREDS, headers=ORIGIN).status_code == 403


def test_loopback_client_detection():
    for ok in ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"]:
        assert is_loopback_client(ok)
    for bad in [None, "", "0.0.0.0", "192.168.0.1", "::ffff:10.0.0.1", "localhost", "::"]:
        assert not is_loopback_client(bad)


def test_host_name_parsing():
    assert host_name("127.0.0.1:8000") == "127.0.0.1"
    assert host_name("LOCALHOST") == "localhost"
    assert host_name("[::1]:5173") == "::1"
    assert host_name(None) is None and host_name("[::1") is None and host_name("a:b:c") is None


# ---------------------------------------------------------------- content type
@pytest.mark.parametrize(
    "content_type",
    ["application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain", None],
)
def test_unsafe_api_bodies_must_be_json(client, admin, content_type):
    headers = dict(ORIGIN)
    if content_type:
        headers["Content-Type"] = content_type
    r = client.post("/api/auth/login", content=b'{"username":"a","password":"b"}', headers=headers)
    assert r.status_code == 415 and r.json() == {"code": "unsupported_media_type"}


def test_json_with_charset_is_accepted(client, admin):
    r = client.post(
        "/api/auth/login",
        content=json.dumps(CREDS).encode(),
        headers={**ORIGIN, "Content-Type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 200
