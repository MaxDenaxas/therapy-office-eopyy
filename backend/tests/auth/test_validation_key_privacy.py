"""Unexpected JSON keys are never echoed in validation errors (privacy hardening after M5.2).

Pydantic reports an ``extra_forbidden`` error at the location of the unexpected key, so the key
itself used to appear in the 422 body (``{"loc": ["body", "<key>"]}``). A key is client data and
could be an AMKA or a name. ``app/errors.py`` now drops that last element centrally; known-field
errors keep their full location, and the response shape and messages are unchanged.

Values are synthetic and built at runtime (no 11-digit literal in the source).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from app.auth.dependencies import protected_router
from tests.auth.conftest import unsafe_headers
from tests.local_http import ORIGIN
from tests.patients.amka import synthetic_amka

AMKA_KEY = synthetic_amka(424242424)
NAME_KEY = "Ξανθίππη " + "Δοκιμαστοπούλου"  # an invented Greek full name
KEYS = [AMKA_KEY, NAME_KEY]
EXTRA_MSG = "Extra inputs are not permitted"
PATIENT_MSG = "Value error, unknown fields are not allowed"  # the patient schemas' own guard

TYPE = {"code": "keypriv", "name_el": "Λογοθεραπεία", "template_label": "Λογοθεραπεία:"}
PERSON = {"full_name": "Θεραπεύτρια Δοκιμής"}
PATIENT = {
    "first_name": "Γεώργιος",
    "last_name": "Δοκιμαστικός",
    "father_name": "Νικόλαος",
    "amka": synthetic_amka(1),
}


def assert_not_echoed(response, key):
    assert response.status_code == 422, response.text
    assert key not in response.text  # nowhere in the body: loc, msg or any other field
    body = response.json()
    assert body["code"] == "validation_error"
    assert set(body) == {"code", "errors"}
    for err in body["errors"]:
        assert set(err) == {"loc", "msg"}
        assert key not in err["loc"] and key not in err["msg"]
    return body["errors"]


@pytest.fixture()
def ids(client, logged_in):
    h = unsafe_headers(logged_in)
    tt = client.post("/api/therapy-types", json=TYPE, headers=h)
    th = client.post("/api/therapists", json=PERSON, headers=h)
    pt = client.post("/api/patients", json=PATIENT, headers=h)
    assert tt.status_code == th.status_code == pt.status_code == 201
    return tt.json()["item"]["id"], th.json()["item"]["id"], pt.json()["item"]["id"]


# ---------------------------------------------------------------- M5.1 reference data
@pytest.mark.parametrize("key", KEYS)
def test_reference_data_routes_never_echo_an_unexpected_key(client, logged_in, ids, key):
    tt, th, _ = ids
    h = unsafe_headers(logged_in)
    requests = [
        ("POST", "/api/therapy-types", {**TYPE, "code": "other", key: 1}),
        ("PATCH", f"/api/therapy-types/{tt}", {"name_el": "Νέο", key: 1}),
        ("POST", "/api/therapists", {**PERSON, key: 1}),
        ("PATCH", f"/api/therapists/{th}", {"specialty": "x", key: "y"}),
    ]
    for method, path, body in requests:
        errors = assert_not_echoed(client.request(method, path, json=body, headers=h), key)
        assert errors == [{"loc": ["body"], "msg": EXTRA_MSG}], (method, path)


# ---------------------------------------------------------------- M5.0 public login
@pytest.mark.parametrize("key", KEYS)
def test_the_public_login_never_echoes_an_unexpected_key(client, admin, key):
    body = {"username": "office-admin", "password": "x" * 12, key: 1}
    r = client.post("/api/auth/login", json=body, headers=ORIGIN)  # no session needed
    assert assert_not_echoed(r, key) == [{"loc": ["body"], "msg": EXTRA_MSG}]


# ---------------------------------------------------------------- M5.2 patients (unchanged)
@pytest.mark.parametrize("key", KEYS)
def test_patient_routes_keep_their_own_protection(client, logged_in, ids, key):
    _, _, pid = ids
    h = unsafe_headers(logged_in)
    requests = [
        ("POST", "/api/patients", {**PATIENT, "amka": synthetic_amka(2), key: 1}),
        ("PATCH", f"/api/patients/{pid}", {"notes": "x", key: 1}),
        ("POST", "/api/patients/search", {"query": "γεωργ", key: 1}),
        ("POST", f"/api/patients/{pid}/amka", {"amka": synthetic_amka(3), key: 1}),
        ("POST", f"/api/patients/{pid}/deactivate", {key: 1}),
    ]
    for method, path, body in requests:
        errors = assert_not_echoed(client.request(method, path, json=body, headers=h), key)
        assert errors == [{"loc": ["body"], "msg": PATIENT_MSG}], (method, path)


# ---------------------------------------------------------------- known fields keep their location
def test_errors_on_known_fields_keep_their_location(client, logged_in, admin, ids):
    tt, th, _ = ids
    h = unsafe_headers(logged_in)
    r = client.post("/api/therapists", json={}, headers=h)
    assert r.status_code == 422
    assert r.json()["errors"] == [{"loc": ["body", "full_name"], "msg": "Field required"}]

    r = client.patch(f"/api/therapy-types/{tt}", json={"code": "Not Valid"}, headers=h)
    assert [e["loc"] for e in r.json()["errors"]] == [["body", "code"]]

    r = client.patch(f"/api/therapists/{th}", json={"license_date": "not-a-date"}, headers=h)
    assert [e["loc"] for e in r.json()["errors"]] == [["body", "license_date"]]

    r = client.post("/api/auth/login", json={"username": 5, "password": "x" * 12}, headers=ORIGIN)
    assert [e["loc"] for e in r.json()["errors"]] == [["body", "username"]]

    r = client.post("/api/patients", json={**PATIENT, "amka": "12"}, headers=h)
    assert [e["loc"] for e in r.json()["errors"]] == [["body", "amka"]]


def test_a_known_field_error_and_an_unexpected_key_together(client, logged_in, ids):
    r = client.post(
        "/api/therapists",
        json={"full_name": 5, AMKA_KEY: 1},
        headers=unsafe_headers(logged_in),
    )
    errors = assert_not_echoed(r, AMKA_KEY)
    assert {"loc": ["body", "full_name"], "msg": "Input should be a valid string"} in errors
    assert {"loc": ["body"], "msg": EXTRA_MSG} in errors
    assert len(errors) == 2


def test_query_and_path_errors_are_unchanged(client, logged_in):
    r = client.get("/api/therapists?active=maybe")
    assert [e["loc"] for e in r.json()["errors"]] == [["query", "active"]]
    r = client.get("/api/therapists/abc")
    assert [e["loc"] for e in r.json()["errors"]] == [["path", "therapist_id"]]


# ---------------------------------------------------------------- nesting and limits (test routes)
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Level5(_Strict):
    v: int = 0


class Level4(_Strict):
    e: Level5 = Level5()


class Level3(_Strict):
    d: Level4 = Level4()


class Level2(_Strict):
    c: Level3 = Level3()


class Level1(_Strict):
    b: Level2 = Level2()


class Root(_Strict):
    a: Level1 = Level1()
    n: int = 0


@pytest.fixture()
def nested_route(app):
    router = protected_router(prefix="/api/test-validation")

    @router.post("/nested")
    def nested(body: Root):
        return {}

    app.include_router(router)
    return "/api/test-validation/nested"


@pytest.mark.parametrize("key", KEYS)
def test_a_nested_unexpected_key_keeps_the_parent_location(client, logged_in, nested_route, key):
    h = unsafe_headers(logged_in)
    r = client.post(nested_route, json={"a": {key: 1}}, headers=h)
    assert assert_not_echoed(r, key) == [{"loc": ["body", "a"], "msg": EXTRA_MSG}]


def test_a_deep_location_is_cut_after_the_key_is_dropped(client, logged_in, nested_route):
    """body.a.b.c.d.e.<key> has 7 parts. Dropping the key *before* the 6-part cut leaves all six
    declared parts; cutting first and then dropping the last part would lose "e" instead."""
    deep = {"a": {"b": {"c": {"d": {"e": {AMKA_KEY: 1}}}}}}
    r = client.post(nested_route, json=deep, headers=unsafe_headers(logged_in))
    errors = assert_not_echoed(r, AMKA_KEY)
    assert errors == [{"loc": ["body", "a", "b", "c", "d", "e"], "msg": EXTRA_MSG}]
    # a known field that deep still keeps its location, up to the existing 6-part limit
    deep_known = {"a": {"b": {"c": {"d": {"e": {"v": "x"}}}}}}
    r = client.post(nested_route, json=deep_known, headers=unsafe_headers(logged_in))
    assert [e["loc"] for e in r.json()["errors"]] == [["body", "a", "b", "c", "d", "e"]]


def test_the_error_count_limit_still_applies_and_no_key_leaks(client, logged_in, nested_route):
    keys = [synthetic_amka(900000000 + i) for i in range(25)]
    r = client.post(nested_route, json={k: 1 for k in keys}, headers=unsafe_headers(logged_in))
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert len(errors) == 20  # the existing cap
    assert all(e == {"loc": ["body"], "msg": EXTRA_MSG} for e in errors)
    assert not any(k in r.text for k in keys)
