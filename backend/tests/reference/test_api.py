"""Therapy-type and therapist API (M5.1): authentication, CSRF, validation, status codes,
conflicts, active-template protection, audit and privacy."""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import select

from app.models import AuditLog, Therapist, TherapyType
from app.services import reference as svc
from app.services.templates import deactivate_template
from tests.auth.conftest import unsafe_headers
from tests.local_http import ORIGIN, local_client

TT = "/api/therapy-types"
TH = "/api/therapists"

TYPE = {"code": "speech2", "name_el": "Λογοθεραπεία Β", "template_label": "Λογοθεραπεία Β:"}
PERSON = {"full_name": "Θεραπεύτρια Δοκιμής Α"}

ALL_ROUTES = [
    ("GET", TT),
    ("POST", TT),
    ("GET", f"{TT}/1"),
    ("PATCH", f"{TT}/1"),
    ("POST", f"{TT}/1/activate"),
    ("POST", f"{TT}/1/deactivate"),
    ("GET", TH),
    ("POST", TH),
    ("GET", f"{TH}/1"),
    ("PATCH", f"{TH}/1"),
    ("POST", f"{TH}/1/activate"),
    ("POST", f"{TH}/1/deactivate"),
]


def send(client, method, path, csrf, body=None):
    kwargs = {"headers": unsafe_headers(csrf)} if method != "GET" else {}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


def create_type(client, csrf, **kw):
    r = client.post(TT, json={**TYPE, **kw}, headers=unsafe_headers(csrf))
    assert r.status_code == 201, r.text
    return r.json()["item"]


def create_person(client, csrf, **kw):
    r = client.post(TH, json={**PERSON, **kw}, headers=unsafe_headers(csrf))
    assert r.status_code == 201, r.text
    return r.json()


def audit(db):
    """Audit rows of the two entities under test (the login fixtures write their own)."""
    return db(
        lambda s: [
            tuple(r)
            for r in s.execute(
                select(AuditLog.action, AuditLog.entity, AuditLog.entity_id, AuditLog.details)
                .where(AuditLog.entity.in_(["therapy_types", "therapists"]))
                .order_by(AuditLog.id)
            ).all()
        ]
    )


# ================================================================ authentication and CSRF
@pytest.mark.parametrize("method, path", ALL_ROUTES)
def test_every_route_requires_a_session(client, admin, method, path):
    headers = dict(ORIGIN) if method != "GET" else {}
    r = client.request(
        method, path, headers=headers, json={} if method in ("POST", "PATCH") else None
    )
    assert r.status_code == 401 and r.json() == {"code": "not_authenticated"}


@pytest.mark.parametrize("method, path", [r for r in ALL_ROUTES if r[0] != "GET"])
def test_every_unsafe_route_requires_the_csrf_token(client, logged_in, method, path):
    body = {"name_el": "x"} if method == "PATCH" else None
    for csrf in (None, "", "x" * 43, logged_in[:-1]):
        r = send(client, method, path, csrf, body)
        assert r.status_code == 403 and r.json() == {"code": "csrf_failed"}, (csrf, r.text)


def test_reads_need_a_session_but_no_csrf_token(client, logged_in):
    assert client.get(TT).status_code == 200
    assert client.get(TH).status_code == 200


def test_the_routes_are_registered_on_protected_routers(app):
    from fastapi.routing import iter_route_contexts

    paths = {r.path for r in iter_route_contexts(app.routes)}
    assert {TT, TH, f"{TT}/{{type_id}}/deactivate", f"{TH}/{{therapist_id}}/activate"} <= paths


def test_there_is_no_delete_endpoint(client, logged_in):
    for path in (f"{TT}/1", f"{TH}/1", TT, TH):
        r = client.delete(path, headers=unsafe_headers(logged_in))
        assert r.status_code == 405 and r.json() == {"code": "method_not_allowed"}


# ================================================================ therapy types
def test_create_and_read_a_therapy_type(client, logged_in):
    item = create_type(
        client,
        logged_in,
        eopyy_code="530000010",
        eopyy_description="περιγραφή",
        supports_home_visit=True,
    )
    assert item == {
        "id": item["id"],
        "code": "speech2",
        "name_el": "Λογοθεραπεία Β",
        "template_label": "Λογοθεραπεία Β:",
        "eopyy_code": "530000010",
        "eopyy_description": "περιγραφή",
        "supports_home_visit": True,
        "active": True,
    }
    got = client.get(f"{TT}/{item['id']}")
    assert got.status_code == 200 and got.json() == {"item": item, "warnings": []}


def test_create_defaults_and_response_shape(client, logged_in):
    r = client.post(TT, json=TYPE, headers=unsafe_headers(logged_in))
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"item", "warnings"} and body["warnings"] == []
    assert body["item"]["eopyy_code"] is None and body["item"]["supports_home_visit"] is False


def test_list_therapy_types_with_active_filter(client, logged_in):
    a = create_type(client, logged_in, code="a1", name_el="Β")
    b = create_type(client, logged_in, code="b1", name_el="Α")
    client.post(f"{TT}/{a['id']}/deactivate", headers=unsafe_headers(logged_in))
    assert [i["id"] for i in client.get(TT).json()["items"]] == [b["id"], a["id"]]
    assert [i["id"] for i in client.get(TT, params={"active": "true"}).json()["items"]] == [b["id"]]
    assert [i["id"] for i in client.get(TT, params={"active": "false"}).json()["items"]] == [
        a["id"]
    ]
    assert client.get(TT, params={"active": "maybe"}).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {**TYPE, "code": "Speech"},  # upper case
        {**TYPE, "code": "1speech"},  # starts with a digit
        {**TYPE, "code": "sp eech"},
        {**TYPE, "code": "λογος"},  # not ASCII
        {**TYPE, "code": "a" * 65},
        {**TYPE, "name_el": "  "},
        {**TYPE, "name_el": "x" * 201},
        {**TYPE, "template_label": ""},
        {**TYPE, "template_label": "x" * 101},
        {**TYPE, "eopyy_code": "53A0"},
        {**TYPE, "eopyy_code": "1" * 17},
        {**TYPE, "eopyy_description": "x" * 201},
        {**TYPE, "supports_home_visit": "maybe"},
        {"code": "x", "name_el": "x"},  # template_label missing
        {},
    ],
)
def test_create_therapy_type_validation_returns_422(client, logged_in, body):
    r = client.post(TT, json=body, headers=unsafe_headers(logged_in))
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    assert client.get(TT).json() == {"items": []}


def test_unknown_request_fields_are_rejected(client, logged_in):
    r = client.post(TT, json={**TYPE, "active": False, "id": 5}, headers=unsafe_headers(logged_in))
    assert r.status_code == 422
    item = create_type(client, logged_in)
    for body in ({"active": False}, {"id": 99}, {"name_el": "ok", "extra": 1}):
        r = client.patch(f"{TT}/{item['id']}", json=body, headers=unsafe_headers(logged_in))
        assert r.status_code == 422


def test_validation_errors_do_not_echo_values(client, logged_in):
    secret = "ΜΥΣΤΙΚΟΣ-ΚΩΔΙΚΟΣ"
    r = client.post(
        TT,
        json={**TYPE, "code": secret, "unknown_field": secret},
        headers=unsafe_headers(logged_in),
    )
    assert r.status_code == 422 and secret not in r.text
    r = client.post(TH, json={**PERSON, "license_date": secret}, headers=unsafe_headers(logged_in))
    assert r.status_code == 422 and secret not in r.text


def test_duplicate_code_and_eopyy_code_answer_409_without_the_value(client, logged_in):
    create_type(client, logged_in, code="onlyone", eopyy_code="987654321")
    r = client.post(TT, json={**TYPE, "code": "onlyone"}, headers=unsafe_headers(logged_in))
    assert r.status_code == 409 and r.json()["code"] == "duplicate_code"
    assert "onlyone" not in r.text
    r = client.post(
        TT,
        json={**TYPE, "code": "other", "eopyy_code": "987654321"},
        headers=unsafe_headers(logged_in),
    )
    assert r.status_code == 409 and r.json()["code"] == "duplicate_eopyy_code"
    assert "987654321" not in r.text
    assert len(client.get(TT).json()["items"]) == 1  # the failed creations left nothing behind


def test_update_therapy_type(client, logged_in):
    item = create_type(client, logged_in, eopyy_code="530000010")
    r = client.patch(
        f"{TT}/{item['id']}",
        json={"name_el": "Νέο όνομα", "eopyy_code": None, "supports_home_visit": True},
        headers=unsafe_headers(logged_in),
    )
    assert r.status_code == 200
    new = r.json()["item"]
    assert new["name_el"] == "Νέο όνομα" and new["eopyy_code"] is None
    assert new["supports_home_visit"] is True and new["template_label"] == item["template_label"]
    assert new["code"] == item["code"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name_el": None},
        {"name_el": ""},
        {"code": None},
        {"supports_home_visit": None},
        {"template_label": "  "},
        {"code": "BAD"},
    ],
)
def test_update_therapy_type_validation(client, logged_in, body):
    item = create_type(client, logged_in)
    r = client.patch(f"{TT}/{item['id']}", json=body, headers=unsafe_headers(logged_in))
    assert r.status_code == 422
    assert client.get(f"{TT}/{item['id']}").json()["item"] == item


def test_update_conflicts_answer_409(client, logged_in):
    a = create_type(client, logged_in, code="a1", eopyy_code="111")
    b = create_type(client, logged_in, code="b1", eopyy_code="222")
    for body, code in (
        ({"code": "a1"}, "duplicate_code"),
        ({"eopyy_code": "111"}, "duplicate_eopyy_code"),
    ):
        r = client.patch(f"{TT}/{b['id']}", json=body, headers=unsafe_headers(logged_in))
        assert r.status_code == 409 and r.json()["code"] == code
    assert (
        client.patch(
            f"{TT}/{a['id']}", json={"eopyy_code": "111"}, headers=unsafe_headers(logged_in)
        ).status_code
        == 200
    )


def test_the_name_is_not_unique_but_warns(client, logged_in):
    a = create_type(client, logged_in, code="a1")
    r = client.post(TT, json={**TYPE, "code": "a2"}, headers=unsafe_headers(logged_in))
    assert r.status_code == 201
    assert r.json()["warnings"] == [{"code": "duplicate_name", "ids": [a["id"]]}]


def test_code_can_change_until_something_uses_the_type(client, logged_in, active_center):
    fresh = create_type(client, logged_in, code="fresh")
    ok = client.patch(
        f"{TT}/{fresh['id']}", json={"code": "renamed"}, headers=unsafe_headers(logged_in)
    )
    assert ok.status_code == 200 and ok.json()["item"]["code"] == "renamed"
    used = active_center.type_ids["speech"]
    r = client.patch(f"{TT}/{used}", json={"code": "renamed"}, headers=unsafe_headers(logged_in))
    assert r.status_code == 409 and r.json()["code"] == "code_locked"
    assert r.json()["referenced_by"] == ["template_configuration_items"]
    assert "create a new therapy type" in r.json()["message"]
    other = client.patch(f"{TT}/{used}", json={"name_el": "Νέο"}, headers=unsafe_headers(logged_in))
    assert other.status_code == 200


def test_activate_and_deactivate_a_therapy_type(client, logged_in, db):
    item = create_type(client, logged_in)
    r = client.post(f"{TT}/{item['id']}/deactivate", headers=unsafe_headers(logged_in))
    assert r.status_code == 200 and r.json()["item"]["active"] is False
    assert (
        client.post(f"{TT}/{item['id']}/deactivate", headers=unsafe_headers(logged_in)).status_code
        == 200
    )
    r = client.post(f"{TT}/{item['id']}/activate", headers=unsafe_headers(logged_in))
    assert r.status_code == 200 and r.json()["item"]["active"] is True
    actions = [a for a, *_ in audit(db)]
    assert actions == ["therapy_type.created", "therapy_type.deactivated", "therapy_type.activated"]


@pytest.mark.parametrize(
    "method, suffix", [("GET", ""), ("PATCH", ""), ("POST", "/activate"), ("POST", "/deactivate")]
)
@pytest.mark.parametrize("base", [TT, TH])
def test_unknown_ids_answer_404(client, logged_in, base, method, suffix):
    body = (
        {"specialty": "x"}
        if method == "PATCH" and base == TH
        else {"name_el": "x"}
        if method == "PATCH"
        else None
    )
    r = send(client, method, f"{base}/4242{suffix}", logged_in, body)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "99999999999999999999"])
@pytest.mark.parametrize("base", [TT, TH])
def test_invalid_or_oversized_ids_never_cause_a_500(client, logged_in, base, bad):
    r = client.get(f"{base}/{bad}")
    assert r.status_code in (404, 422), r.text


# ================================================================ therapists
def test_create_and_read_a_therapist(client, logged_in):
    body = create_person(
        client,
        logged_in,
        specialty="Λογοθεραπεία",
        license_no="TEST-1",
        license_authority="Δοκιμαστική Αρχή",
        license_date="2020-01-31",
    )
    item = body["item"]
    assert body["warnings"] == []
    assert item["active"] is True and item["license_date"] == "2020-01-31"
    assert item["created_at"]  # set by the database
    assert client.get(f"{TH}/{item['id']}").json() == {"item": item, "warnings": []}
    minimal = create_person(client, logged_in, full_name="Μόνο Όνομα")["item"]
    assert (
        minimal["specialty"] is None
        and minimal["license_no"] is None
        and minimal["license_date"] is None
    )


@pytest.mark.parametrize(
    "body",
    [
        {"full_name": "  "},
        {"full_name": "x" * 201},
        {**PERSON, "specialty": "x" * 201},
        {**PERSON, "license_no": "x" * 65},
        {**PERSON, "license_date": "31/01/2020"},
        {**PERSON, "license_date": "2020-02-30"},
        {**PERSON, "active": False},
        {**PERSON, "id": 3},
        {"specialty": "x"},
        {},
    ],
)
def test_create_therapist_validation_returns_422(client, logged_in, body):
    r = client.post(TH, json=body, headers=unsafe_headers(logged_in))
    assert r.status_code == 422
    assert client.get(TH).json() == {"items": []}


def test_duplicate_names_and_licence_numbers_warn_but_succeed(client, logged_in):
    first = create_person(client, logged_in, full_name="Μαρία Παπά", license_no="TEST-7")["item"]
    second = create_person(client, logged_in, full_name="ΜΑΡΙΑ ΠΑΠΑ", license_no="test-7")
    assert second["item"]["id"] != first["id"]
    assert second["warnings"] == [
        {"code": "duplicate_full_name", "ids": [first["id"]]},
        {"code": "duplicate_license_no", "ids": [first["id"]]},
    ]
    assert len(client.get(TH).json()["items"]) == 2


def test_update_therapist(client, logged_in):
    item = create_person(client, logged_in, license_no="TEST-1")["item"]
    r = client.patch(
        f"{TH}/{item['id']}",
        json={"specialty": "Εργοθεραπεία", "license_date": "2019-05-06", "license_no": None},
        headers=unsafe_headers(logged_in),
    )
    assert r.status_code == 200
    new = r.json()["item"]
    assert new["specialty"] == "Εργοθεραπεία" and new["license_date"] == "2019-05-06"
    assert new["license_no"] is None and new["full_name"] == item["full_name"]


@pytest.mark.parametrize(
    "body", [{}, {"full_name": None}, {"full_name": " "}, {"license_date": "bad"}, {"x": 1}]
)
def test_update_therapist_validation(client, logged_in, body):
    item = create_person(client, logged_in)["item"]
    r = client.patch(f"{TH}/{item['id']}", json=body, headers=unsafe_headers(logged_in))
    assert r.status_code == 422
    assert client.get(f"{TH}/{item['id']}").json()["item"] == item


def test_update_therapist_can_warn_about_a_new_duplicate(client, logged_in):
    a = create_person(client, logged_in, full_name="Α Α")["item"]
    b = create_person(client, logged_in, full_name="Β Β")["item"]
    r = client.patch(
        f"{TH}/{b['id']}", json={"full_name": "α α"}, headers=unsafe_headers(logged_in)
    )
    assert r.status_code == 200
    assert r.json()["warnings"] == [{"code": "duplicate_full_name", "ids": [a["id"]]}]


def test_list_and_filter_therapists(client, logged_in):
    a = create_person(client, logged_in, full_name="Β")["item"]
    b = create_person(client, logged_in, full_name="Α")["item"]
    client.post(f"{TH}/{a['id']}/deactivate", headers=unsafe_headers(logged_in))
    assert [i["id"] for i in client.get(TH).json()["items"]] == [b["id"], a["id"]]
    assert [i["id"] for i in client.get(TH, params={"active": "true"}).json()["items"]] == [b["id"]]
    assert [i["id"] for i in client.get(TH, params={"active": "false"}).json()["items"]] == [
        a["id"]
    ]


def test_activate_and_deactivate_a_therapist(client, logged_in, db):
    item = create_person(client, logged_in)["item"]
    r = client.post(f"{TH}/{item['id']}/deactivate", headers=unsafe_headers(logged_in))
    assert r.status_code == 200 and r.json()["item"]["active"] is False
    r = client.post(f"{TH}/{item['id']}/activate", headers=unsafe_headers(logged_in))
    assert r.status_code == 200 and r.json()["item"]["active"] is True
    assert [a for a, *_ in audit(db)] == [
        "therapist.created",
        "therapist.deactivated",
        "therapist.activated",
    ]


# ================================================================ active-template protection
def test_a_therapy_type_in_an_active_template_cannot_be_deactivated(
    client, logged_in, active_center
):
    tid = active_center.type_ids["speech"]
    r = client.post(f"{TT}/{tid}/deactivate", headers=unsafe_headers(logged_in))
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "in_use_by_active_template"
    assert [t["id"] for t in body["templates"]] == [active_center.template_id]
    assert "deactivate it first" in body["message"]
    assert client.get(f"{TT}/{tid}").json()["item"]["active"] is True


def test_a_therapist_in_an_active_template_cannot_be_deactivated(client, logged_in, active_center):
    tid = active_center.therapist_ids["occupational"]
    r = client.post(f"{TH}/{tid}/deactivate", headers=unsafe_headers(logged_in))
    assert r.status_code == 409 and r.json()["code"] == "in_use_by_active_template"
    assert "corrected template version" in r.json()["message"]
    assert client.get(f"{TH}/{tid}").json()["item"]["active"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"full_name": "Άλλο Όνομα"},
        {"license_no": "TEST-999"},
        {"license_authority": "Άλλη Αρχή"},
        {"license_date": "2001-01-01"},
        {"license_no": None},
    ],
    ids=lambda b: next(iter(b)),
)
def test_identity_fields_cannot_change_under_an_active_template(
    client, logged_in, active_center, body
):
    tid = active_center.therapist_ids["speech"]
    before = client.get(f"{TH}/{tid}").json()["item"]
    r = client.patch(f"{TH}/{tid}", json=body, headers=unsafe_headers(logged_in))
    assert r.status_code == 409 and r.json()["code"] == "identity_locked"
    assert r.json()["templates"][0]["id"] == active_center.template_id
    assert client.get(f"{TH}/{tid}").json()["item"] == before


def test_non_identity_edits_are_allowed_under_an_active_template(client, logged_in, active_center):
    tid = active_center.therapist_ids["speech"]
    r = client.patch(
        f"{TH}/{tid}", json={"specialty": "Νέα ειδικότητα"}, headers=unsafe_headers(logged_in)
    )
    assert r.status_code == 200 and r.json()["item"]["specialty"] == "Νέα ειδικότητα"


def test_after_deactivating_the_template_the_changes_are_allowed(
    client, logged_in, active_center, db
):
    th, tt = active_center.therapist_ids["speech"], active_center.type_ids["speech"]
    assert (
        client.patch(
            f"{TH}/{th}", json={"license_no": "TEST-NEW"}, headers=unsafe_headers(logged_in)
        ).status_code
        == 409
    )
    db(lambda s: deactivate_template(s, active_center.template_id))
    assert (
        client.patch(
            f"{TH}/{th}", json={"license_no": "TEST-NEW"}, headers=unsafe_headers(logged_in)
        ).status_code
        == 200
    )
    assert (
        client.post(f"{TH}/{th}/deactivate", headers=unsafe_headers(logged_in)).status_code == 200
    )
    assert (
        client.post(f"{TT}/{tt}/deactivate", headers=unsafe_headers(logged_in)).status_code == 200
    )


def test_a_database_rule_that_slips_past_the_service_is_a_409_not_a_500(
    client, logged_in, active_center, monkeypatch, db
):
    monkeypatch.setattr(svc, "_refuse_while_in_active_template", lambda *a, **k: None)
    tid = active_center.therapist_ids["speech"]
    r = client.post(f"{TH}/{tid}/deactivate", headers=unsafe_headers(logged_in))
    assert r.status_code == 409 and r.json()["code"] == "in_use_by_active_template"
    assert "sqlite" not in r.text.lower() and "Traceback" not in r.text
    r = client.patch(f"{TH}/{tid}", json={"full_name": "Νέο"}, headers=unsafe_headers(logged_in))
    assert r.status_code == 409 and r.json()["code"] == "identity_locked" and "Νέο" not in r.text
    assert db(lambda s: s.get(Therapist, tid).active) is True


def test_the_template_names_in_errors_are_the_only_data_returned(client, logged_in, active_center):
    r = client.post(
        f"{TH}/{active_center.therapist_ids['speech']}/deactivate",
        headers=unsafe_headers(logged_in),
    )
    body = r.json()
    assert set(body) == {"code", "message", "templates"}
    assert set(body["templates"][0]) == {"id", "name"}


# ================================================================ audit and privacy
def test_audit_entries_identify_the_entity_and_field_names_only(client, logged_in, db):
    person = create_person(
        client, logged_in, full_name="Μυστική Ονομασία", license_no="TEST-SECRET"
    )["item"]
    client.patch(
        f"{TH}/{person['id']}",
        json={"full_name": "Άλλο Μυστικό", "specialty": "Ειδικότητα Χ"},
        headers=unsafe_headers(logged_in),
    )
    tt = create_type(client, logged_in, code="auditcode", name_el="Όνομα Ελέγχου")
    client.patch(
        f"{TT}/{tt['id']}", json={"eopyy_code": "123456"}, headers=unsafe_headers(logged_in)
    )
    client.post(f"{TH}/{person['id']}/deactivate", headers=unsafe_headers(logged_in))
    rows = audit(db)
    assert [(a, e, i) for a, e, i, _ in rows] == [
        ("therapist.created", "therapists", person["id"]),
        ("therapist.updated", "therapists", person["id"]),
        ("therapy_type.created", "therapy_types", tt["id"]),
        ("therapy_type.updated", "therapy_types", tt["id"]),
        ("therapist.deactivated", "therapists", person["id"]),
    ]
    assert json.loads(rows[1][3]) == {"fields": ["full_name", "specialty"]}
    assert json.loads(rows[3][3]) == {"fields": ["eopyy_code"]}
    dump = json.dumps(rows, ensure_ascii=False)
    for secret in (
        "Μυστική",
        "TEST-SECRET",
        "Άλλο",
        "Ειδικότητα",
        "auditcode",
        "Όνομα Ελέγχου",
        "123456",
    ):
        assert secret not in dump


def test_failed_requests_leave_no_audit_rows_and_no_partial_writes(client, logged_in, db):
    create_type(client, logged_in, code="dup1")
    before = audit(db)
    client.post(TT, json={**TYPE, "code": "dup1"}, headers=unsafe_headers(logged_in))
    client.post(TT, json={}, headers=unsafe_headers(logged_in))
    client.patch(f"{TT}/9999", json={"name_el": "x"}, headers=unsafe_headers(logged_in))
    assert audit(db) == before


def test_logs_never_contain_the_submitted_values(client, logged_in, caplog):
    caplog.set_level(logging.DEBUG)
    secrets_ = ("ΜΥΣΤΙΚΟ-ΟΝΟΜΑ", "TEST-LOGSECRET", "logsecretcode", "55555555")
    client.post(
        TH,
        json={"full_name": secrets_[0], "license_no": secrets_[1]},
        headers=unsafe_headers(logged_in),
    )
    client.post(
        TH, json={"full_name": secrets_[0], "unknown": 1}, headers=unsafe_headers(logged_in)
    )
    client.post(
        TT,
        json={**TYPE, "code": secrets_[2], "eopyy_code": secrets_[3]},
        headers=unsafe_headers(logged_in),
    )
    client.post(
        TT,
        json={**TYPE, "code": secrets_[2], "eopyy_code": secrets_[3]},
        headers=unsafe_headers(logged_in),
    )
    for secret in secrets_:
        assert secret not in caplog.text


def test_non_json_bodies_are_refused_before_reaching_the_service(client, logged_in):
    r = client.post(
        TT,
        content="code=x&name_el=y&template_label=z",
        headers={**unsafe_headers(logged_in), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415
    assert client.get(TT).json() == {"items": []}


def test_responses_carry_the_api_security_headers(client, logged_in):
    r = client.get(TT)
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_another_origin_cannot_use_the_routes(app, logged_in, client):
    cookies = dict(client.cookies)
    with local_client(app, cookies=cookies) as c:
        r = c.post(
            TT, json=TYPE, headers={"Origin": "http://evil.example", "X-CSRF-Token": logged_in}
        )
        assert r.status_code == 403 and r.json() == {"code": "origin_rejected"}


def test_data_persists_across_requests(client, logged_in, db):
    item = create_type(client, logged_in, code="persist")
    assert db(lambda s: s.get(TherapyType, item["id"]).code) == "persist"


# ================================================================ audit follow-ups
INVALID_IDS = ["0", "-5", "abc", "1.5", "9223372036854775808", "99999999999999999999"]


@pytest.mark.parametrize("bad", INVALID_IDS)
@pytest.mark.parametrize("base", [TT, TH])
def test_invalid_and_oversized_ids_on_state_changing_routes_are_never_a_500(
    client, logged_in, db, base, bad
):
    before = audit(db)
    body = {"name_el": "x"} if base == TT else {"specialty": "x"}
    responses = [
        client.patch(f"{base}/{bad}", json=body, headers=unsafe_headers(logged_in)),
        client.post(f"{base}/{bad}/activate", headers=unsafe_headers(logged_in)),
        client.post(f"{base}/{bad}/deactivate", headers=unsafe_headers(logged_in)),
    ]
    for r in responses:
        assert r.status_code in (404, 422), (bad, r.status_code, r.text)
        assert r.json()["code"] in ("not_found", "validation_error")
        assert bad not in r.text.replace("loc", "")  # the submitted id is not echoed back
    if bad in ("9223372036854775808", "99999999999999999999"):
        assert all(r.status_code == 422 for r in responses)  # beyond SQLite's integer range
    assert audit(db) == before


@pytest.mark.parametrize("base", [TT, TH])
def test_the_largest_valid_id_is_a_clean_404(client, logged_in, base):
    body = {"name_el": "x"} if base == TT else {"specialty": "x"}
    biggest = "9223372036854775807"
    assert client.get(f"{base}/{biggest}").status_code == 404
    assert (
        client.patch(f"{base}/{biggest}", json=body, headers=unsafe_headers(logged_in)).status_code
        == 404
    )
    assert (
        client.post(f"{base}/{biggest}/deactivate", headers=unsafe_headers(logged_in)).status_code
        == 404
    )


@pytest.mark.parametrize("unique_field", ["code", "eopyy_code"])
def test_concurrent_creates_with_the_same_unique_value_give_one_201_and_one_409(
    app, client, logged_in, unique_field
):
    """Two requests race for the same value: BEGIN IMMEDIATE serialises them, so exactly one
    wins and the other gets a safe 409 (never a 500, never two rows). Repeated with a barrier so
    that the two requests really do start together."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    cookies = dict(client.cookies)
    headers = unsafe_headers(logged_in)

    def attempt(body, barrier):
        racer = local_client(app, cookies=cookies, raise_server_exceptions=False)
        barrier.wait(timeout=15)
        response = racer.post(TT, json=body, headers=headers)
        return response.status_code, response.json()

    for round_no in range(5):
        shared = f"race{round_no}" if unique_field == "code" else f"7700{round_no}"
        bodies = [
            {**TYPE, "code": shared if unique_field == "code" else f"r{round_no}a"},
            {**TYPE, "code": shared if unique_field == "code" else f"r{round_no}b"},
        ]
        if unique_field == "eopyy_code":
            for body in bodies:
                body["eopyy_code"] = shared
        barrier = Barrier(2)
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda b, bar=barrier: attempt(b, bar), bodies))
        statuses = sorted(status for status, _ in outcomes)
        assert statuses == [201, 409], outcomes
        loser = next(body for status, body in outcomes if status == 409)
        assert loser["code"] == f"duplicate_{unique_field}"
        assert shared not in json.dumps(loser)
    items = client.get(TT).json()["items"]
    assert len(items) == 5  # one winner per round, nothing else persisted
