"""Patient API (M5.2): authentication, CSRF, validation, status codes, masking, query-parameter
guard, conflicts and audit."""

from __future__ import annotations

import json

import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy import select, text

from app.models import AuditLog, Patient
from app.schemas.patients import PatientSummary
from tests.db import factories as f
from tests.db.test_periods import issue, setup_period
from tests.local_http import ORIGIN, local_client
from tests.patients.amka import synthetic_amka
from tests.patients.conftest import PATIENTS, person, unsafe_headers

ALL_ROUTES = [
    ("GET", PATIENTS),
    ("POST", PATIENTS),
    ("POST", f"{PATIENTS}/search"),
    ("GET", f"{PATIENTS}/1"),
    ("PATCH", f"{PATIENTS}/1"),
    ("POST", f"{PATIENTS}/1/amka"),
    ("POST", f"{PATIENTS}/1/activate"),
    ("POST", f"{PATIENTS}/1/deactivate"),
]


def post(client, csrf, path, body=None):
    return client.post(path, json=body, headers=unsafe_headers(csrf))


def patch(client, csrf, pid, body):
    return client.patch(f"{PATIENTS}/{pid}", json=body, headers=unsafe_headers(csrf))


def audit(db):
    return db(
        lambda s: [
            (r.action, r.entity_id, r.details)
            for r in s.scalars(
                select(AuditLog).where(AuditLog.entity == "patients").order_by(AuditLog.id)
            )
        ]
    )


# ================================================================ authentication and CSRF
@pytest.mark.parametrize("method, path", ALL_ROUTES)
def test_every_route_requires_a_session(client, admin, method, path):
    headers = dict(ORIGIN) if method != "GET" else {}
    body = {} if method in ("POST", "PATCH") else None
    r = client.request(method, path, headers=headers, json=body)
    assert r.status_code == 401 and r.json() == {"code": "not_authenticated"}


@pytest.mark.parametrize("method, path", [r for r in ALL_ROUTES if r[0] != "GET"])
def test_every_unsafe_route_requires_the_csrf_token(client, logged_in, method, path):
    for csrf in (None, "", "x" * 43, logged_in[:-1]):
        r = client.request(method, path, json={}, headers=unsafe_headers(csrf))
        assert r.status_code == 403 and r.json() == {"code": "csrf_failed"}, csrf


def test_another_origin_is_refused(app, client, logged_in):
    with local_client(app, cookies=dict(client.cookies)) as other:
        r = other.post(
            f"{PATIENTS}/search",
            json={"query": "παπα"},
            headers={"Origin": "http://evil.example", "X-CSRF-Token": logged_in},
        )
    assert r.status_code == 403 and r.json() == {"code": "origin_rejected"}


def test_non_json_bodies_are_refused(client, logged_in):
    r = client.post(
        f"{PATIENTS}/search",
        content="query=x",
        headers={**unsafe_headers(logged_in), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415


def test_there_is_no_delete_endpoint(client, logged_in, create):
    pid = create()["id"]
    for path in (PATIENTS, f"{PATIENTS}/{pid}"):
        r = client.delete(path, headers=unsafe_headers(logged_in))
        assert r.status_code == 405 and r.json() == {"code": "method_not_allowed"}


def test_routes_are_registered(app):
    paths = {r.path for r in iter_route_contexts(app.routes)}
    assert {PATIENTS, f"{PATIENTS}/search", f"{PATIENTS}/{{patient_id}}/amka"} <= paths


# ================================================================ create / read
def test_create_and_read(client, logged_in):
    r = post(client, logged_in, PATIENTS, person(1, notes="  σημείωση\n\tβ "))
    assert r.status_code == 201
    body = r.json()
    item = body["item"]
    assert body["warnings"] == []
    assert set(item) == {
        "id",
        "first_name",
        "last_name",
        "father_name",
        "amka",
        "notes",
        "active",
        "created_at",
        "updated_at",
    }
    assert item["amka"] == synthetic_amka(1) and item["notes"] == "σημείωση\n\tβ"
    detail = client.get(f"{PATIENTS}/{item['id']}").json()
    assert detail["item"] == item  # the detail shows the full AMKA
    assert detail["usage"] == {
        "prescriptions": 0,
        "draft_prescriptions": 0,
        "open_assignments": 0,
        "unissued_periods": 0,
        "issued_documents": 0,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"first_name": ""},
        {"first_name": "Γιώρ\tγος"},
        {"last_name": "Παπα\nδόπουλος"},
        {"father_name": "α" * 101},
        {"father_name": "Νικόλαος\u00a0Β"},
        {"amka": "123"},
        {"amka": "٠" * 11},
        {"notes": "α\rβ"},
        {"notes": "α" * 2001},
        {"first_name": 5},
        {"unexpected": "x"},
    ],
)
def test_create_validation_is_422_without_echo(client, logged_in, change):
    body = {**person(1), **change}
    r = post(client, logged_in, PATIENTS, body)
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    for value in change.values():
        if isinstance(value, str) and len(value) > 2:
            assert value not in r.text


def test_missing_fields_are_422(client, logged_in):
    for missing in ("first_name", "last_name", "father_name", "amka"):
        body = person(1)
        del body[missing]
        assert post(client, logged_in, PATIENTS, body).status_code == 422


def test_duplicate_amka_is_409_with_the_other_id_but_never_the_value(client, logged_in, create):
    first = create(1)
    r = post(client, logged_in, PATIENTS, person(1, first_name="Άλλος"))
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "duplicate_amka"
    assert body["patient_id"] == first["id"] and body["active"] is True
    assert synthetic_amka(1) not in r.text


def test_possible_duplicate_warning(client, logged_in, create):
    first = create(1)
    r = post(client, logged_in, PATIENTS, person(2, first_name="ΓΕΩΡΓΙΟΣ"))
    assert r.json()["warnings"] == [
        {"code": "possible_duplicate", "ids": [first["id"]], "count": None}
    ]


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "9223372036854775808"])
def test_invalid_ids_are_never_a_500(client, logged_in, bad):
    responses = [
        client.get(f"{PATIENTS}/{bad}"),
        patch(client, logged_in, bad, {"notes": "x"}),
        post(client, logged_in, f"{PATIENTS}/{bad}/amka", {"amka": synthetic_amka(1)}),
        post(client, logged_in, f"{PATIENTS}/{bad}/deactivate"),
    ]
    assert all(r.status_code == 422 for r in responses)


def test_unknown_ids_are_404(client, logged_in):
    big = "9223372036854775807"
    assert client.get(f"{PATIENTS}/{big}").status_code == 404
    assert patch(client, logged_in, big, {"notes": "x"}).status_code == 404
    assert post(client, logged_in, f"{PATIENTS}/{big}/activate").status_code == 404
    r = post(client, logged_in, f"{PATIENTS}/{big}/amka", {"amka": synthetic_amka(1)})
    assert r.status_code == 404


# ================================================================ update
def test_patch_changes_given_fields(client, logged_in, create, clock):
    item = create()
    clock.advance(minutes=10)  # within the 30 min idle timeout
    r = patch(client, logged_in, item["id"], {"last_name": " Νέο  Επώνυμο ", "notes": None})
    assert r.status_code == 200
    new = r.json()["item"]
    assert new["last_name"] == "Νέο Επώνυμο" and new["first_name"] == item["first_name"]
    assert new["updated_at"] > item["updated_at"] and new["created_at"] == item["created_at"]


def test_patch_refuses_the_amka_empty_bodies_and_empty_names(client, logged_in, create):
    pid = create()["id"]
    for body in ({"amka": synthetic_amka(2)}, {}, {"first_name": None}, {"last_name": "  "}):
        r = patch(client, logged_in, pid, body)
        assert r.status_code == 422, body
    assert client.get(f"{PATIENTS}/{pid}").json()["item"]["amka"] == synthetic_amka(1)


# ================================================================ AMKA correction
def test_amka_correction(client, logged_in, create, db):
    pid = create(1)["id"]
    r = post(client, logged_in, f"{PATIENTS}/{pid}/amka", {"amka": synthetic_amka(2)})
    assert r.status_code == 200 and r.json()["item"]["amka"] == synthetic_amka(2)
    assert audit(db)[-1] == ("patient.amka_corrected", pid, json.dumps({"issued_documents": 0}))


def test_amka_correction_needs_confirmation_when_documents_were_issued(client, logged_in, db):
    def build(s):
        pat, _, _, thr, per, refs = setup_period(s)
        f.period_therapy(s, per, refs["speech"], thr)
        issue(s, per)
        return pat.id

    pid = db(build)
    path = f"{PATIENTS}/{pid}/amka"
    r = post(client, logged_in, path, {"amka": synthetic_amka(5)})
    assert r.status_code == 409
    assert r.json()["code"] == "amka_change_needs_confirmation"
    assert r.json()["issued_documents"] == 1
    r = post(
        client, logged_in, path, {"amka": synthetic_amka(5), "acknowledge_issued_documents": True}
    )
    assert r.status_code == 200
    assert r.json()["warnings"] == [
        {"code": "issued_documents_show_previous_details", "ids": [], "count": 1}
    ]
    assert client.get(f"{PATIENTS}/{pid}").json()["usage"]["issued_documents"] == 1


def test_amka_correction_validation(client, logged_in, create):
    a, b = create(1)["id"], create(2)["id"]
    path = f"{PATIENTS}/{b}/amka"
    assert post(client, logged_in, path, {"amka": "1"}).status_code == 422
    assert post(client, logged_in, path, {}).status_code == 422
    r = post(client, logged_in, path, {"amka": synthetic_amka(1)})
    assert r.status_code == 409 and r.json()["patient_id"] == a


# ================================================================ lifecycle
def test_deactivate_and_reactivate(client, logged_in, create, db):
    pid = create()["id"]
    r = post(client, logged_in, f"{PATIENTS}/{pid}/deactivate")
    assert r.status_code == 200 and r.json()["item"]["active"] is False
    assert post(client, logged_in, f"{PATIENTS}/{pid}/deactivate", {}).status_code == 200
    assert post(client, logged_in, f"{PATIENTS}/{pid}/activate").json()["item"]["active"] is True
    assert [a for a, _, _ in audit(db)] == [
        "patient.created",
        "patient.deactivated",
        "patient.activated",
    ]
    assert db(lambda s: s.get(Patient, pid)) is not None  # never deleted


def test_deactivate_refuses_a_body_with_fields(client, logged_in, create):
    pid = create()["id"]
    assert post(client, logged_in, f"{PATIENTS}/{pid}/deactivate", {"x": 1}).status_code == 422


def test_an_inactive_amka_conflict_says_so(client, logged_in, create):
    pid = create(1)["id"]
    post(client, logged_in, f"{PATIENTS}/{pid}/deactivate")
    r = post(client, logged_in, PATIENTS, person(1))
    assert r.status_code == 409 and r.json()["active"] is False and r.json()["patient_id"] == pid


# ================================================================ lists and masking
def test_list_masks_the_amka(client, logged_in, create):
    create(123456789)
    r = client.get(PATIENTS)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["offset"] == 0 and body["limit"] == 50
    (row,) = body["items"]
    assert set(row) == {"id", "first_name", "last_name", "father_name", "amka_masked", "active"}
    assert row["amka_masked"] == "•" * 7 + synthetic_amka(123456789)[-4:]
    assert synthetic_amka(123456789) not in r.text


def test_the_summary_schema_cannot_carry_a_full_amka():
    assert "amka" not in PatientSummary.model_fields
    assert "notes" not in PatientSummary.model_fields


def test_list_filter_and_pagination(client, logged_in, create):
    ids = [create(n, last_name=f"Επώνυμο{chr(0x3B1 + n)}")["id"] for n in range(3)]
    page = client.get(f"{PATIENTS}?offset=1&limit=1").json()
    assert page["total"] == 3 and [r["id"] for r in page["items"]] == ids[1:2]
    post(client, logged_in, f"{PATIENTS}/{ids[0]}/deactivate")
    # omitted: active patients only; true: active only; false: inactive only
    assert [r["id"] for r in client.get(PATIENTS).json()["items"]] == ids[1:]
    assert client.get(PATIENTS).json()["total"] == 2
    assert [r["id"] for r in client.get(f"{PATIENTS}?active=true").json()["items"]] == ids[1:]
    assert [r["id"] for r in client.get(f"{PATIENTS}?active=false").json()["items"]] == ids[:1]
    for bad in ("limit=0", "limit=101", "offset=-1", "offset=1000001", "active=maybe"):
        assert client.get(f"{PATIENTS}?{bad}").status_code == 422, bad


@pytest.mark.parametrize(
    "query",
    ["q=x", "search=x", "amka=x", "active=true&active=false", "limit=1&limit=2", "x", "ACTIVE=1"],
)
def test_unknown_or_repeated_query_parameters_are_refused_without_echo(
    client, logged_in, create, query
):
    pid = create()["id"]
    for path in (f"{PATIENTS}?{query}", f"{PATIENTS}/{pid}?{query}"):
        r = client.get(path)
        assert r.status_code == 422, path
        assert r.json() == {"code": "unknown_query_parameter"}


def test_query_parameters_are_refused_on_unsafe_routes_too(client, logged_in, create):
    pid = create()["id"]
    r = client.post(
        f"{PATIENTS}/search?query=x", json={"query": "x"}, headers=unsafe_headers(logged_in)
    )
    assert r.json() == {"code": "unknown_query_parameter"}
    r = client.patch(
        f"{PATIENTS}/{pid}?notes=x", json={"notes": "y"}, headers=unsafe_headers(logged_in)
    )
    assert r.json() == {"code": "unknown_query_parameter"}


# ================================================================ search
def test_search_by_name_and_by_amka(client, logged_in, create):
    a = create(1, first_name="Άννα-Μαρία", last_name="Οικονόμου", father_name="Σταύρος")
    create(2)
    r = post(client, logged_in, f"{PATIENTS}/search", {"query": "μαρια σταυρος"})
    assert r.status_code == 200
    assert [x["id"] for x in r.json()["items"]] == [a["id"]]
    r = post(client, logged_in, f"{PATIENTS}/search", {"amka": synthetic_amka(1)})
    body = r.json()
    assert [x["id"] for x in body["items"]] == [a["id"]]
    assert "amka" not in body["items"][0]  # even an exact AMKA search returns masked rows
    assert synthetic_amka(1) not in r.text


@pytest.mark.parametrize(
    "body, code",
    [
        ({}, "validation_error"),
        ({"query": "x", "amka": synthetic_amka(1)}, "validation_error"),
        ({"query": ""}, "validation_error"),
        ({"query": "α" * 101}, "validation_error"),
        ({"amka": "123"}, "validation_error"),
        ({"query": "%"}, "invalid_query"),
        ({"query": "α β γ δ ε ζ"}, "invalid_query"),
        ({"query": "x", "limit": 101}, "validation_error"),
        ({"query": "x", "offset": -1}, "validation_error"),
        ({"query": "x", "other": 1}, "validation_error"),
    ],
)
def test_search_validation(client, logged_in, body, code):
    r = post(client, logged_in, f"{PATIENTS}/search", body)
    assert r.status_code == 422 and r.json()["code"] == code


def test_search_filters_and_pages(client, logged_in, create):
    ids = [create(n)["id"] for n in range(1, 4)]  # same names
    post(client, logged_in, f"{PATIENTS}/search", {"query": "γεωργιος"})
    r = post(
        client, logged_in, f"{PATIENTS}/search", {"query": "γεωργιος", "offset": 1, "limit": 1}
    )
    assert r.json()["total"] == 3 and [x["id"] for x in r.json()["items"]] == ids[1:2]
    post(client, logged_in, f"{PATIENTS}/{ids[0]}/deactivate")
    r = post(client, logged_in, f"{PATIENTS}/search", {"query": "γεωργιος"})
    assert [x["id"] for x in r.json()["items"]] == ids[1:]  # omitted: active only
    r = post(client, logged_in, f"{PATIENTS}/search", {"query": "γεωργιος", "active": True})
    assert [x["id"] for x in r.json()["items"]] == ids[1:]
    r = post(client, logged_in, f"{PATIENTS}/search", {"query": "γεωργιος", "active": False})
    assert [x["id"] for x in r.json()["items"]] == ids[:1]  # inactive patients stay findable
    r = post(client, logged_in, f"{PATIENTS}/search", {"amka": synthetic_amka(1)})
    assert r.json()["items"] == []
    r = post(client, logged_in, f"{PATIENTS}/search", {"amka": synthetic_amka(1), "active": False})
    assert [x["id"] for x in r.json()["items"]] == ids[:1]
    # "every patient" is not expressible through the API
    r = post(client, logged_in, f"{PATIENTS}/search", {"query": "γεωργιος", "active": None})
    assert r.status_code == 422


def test_search_writes_nothing(client, logged_in, create, db):
    create()
    before = audit(db)
    post(client, logged_in, f"{PATIENTS}/search", {"query": "γεωργιος"})
    assert audit(db) == before


# ================================================================ audit
def test_audit_carries_ids_and_field_names_only(client, logged_in, create, db):
    item = create(1, notes="μυστική σημείωση")
    patch(client, logged_in, item["id"], {"first_name": "Κρυφό", "notes": "άλλη"})
    post(client, logged_in, f"{PATIENTS}/{item['id']}/amka", {"amka": synthetic_amka(2)})
    post(client, logged_in, f"{PATIENTS}/{item['id']}/deactivate")
    rows = audit(db)
    assert [(a, i) for a, i, _ in rows] == [
        ("patient.created", item["id"]),
        ("patient.updated", item["id"]),
        ("patient.amka_corrected", item["id"]),
        ("patient.deactivated", item["id"]),
    ]
    assert json.loads(rows[1][2]) == {"fields": ["first_name", "notes"]}
    dump = json.dumps(rows, ensure_ascii=False)
    for secret in (synthetic_amka(1), synthetic_amka(2), "Κρυφό", "Γεώργιος", "μυστική", "άλλη"):
        assert secret not in dump


def test_failed_requests_leave_no_audit_rows(client, logged_in, create, db):
    create(1)
    before = audit(db)
    post(client, logged_in, PATIENTS, person(1))
    post(client, logged_in, PATIENTS, {})
    patch(client, logged_in, 9999, {"notes": "x"})
    assert audit(db) == before


# ================================================================ review fixes (M5.2)
def _snapshot(db):
    return db(
        lambda s: (
            s.execute(text("SELECT * FROM patients ORDER BY id")).all(),
            s.execute(text("SELECT * FROM audit_log WHERE entity = 'patients' ORDER BY id")).all(),
        )
    )


def _raw(client, csrf, method, path, json_text):
    headers = {**unsafe_headers(csrf), "Content-Type": "application/json"}
    return client.request(method, path, content=json_text.encode("ascii"), headers=headers)


@pytest.mark.parametrize("field", ["first_name", "last_name", "father_name", "notes"])
def test_lone_surrogates_are_a_422_not_a_500_and_write_nothing(
    client, logged_in, create, db, field
):
    pid = create(1)["id"]
    before = _snapshot(db)
    escaped = r'"x\ud800y"'  # a JSON escape for a lone surrogate (valid JSON, invalid text)
    body = {k: v for k, v in person(2).items() if k != field}
    create_json = json.dumps(body)[:-1] + f', "{field}": {escaped}}}'
    r = _raw(client, logged_in, "POST", PATIENTS, create_json)
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    r = _raw(client, logged_in, "PATCH", f"{PATIENTS}/{pid}", f'{{"{field}": {escaped}}}')
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    assert _snapshot(db) == before


def test_a_lone_surrogate_in_a_search_is_a_422(client, logged_in):
    r = _raw(client, logged_in, "POST", f"{PATIENTS}/search", r'{"query": "x\udfff"}')
    assert r.status_code == 422


@pytest.mark.parametrize("name", ["-", "1", "...", "\u0301", "\ue000", "★"])
def test_names_without_a_letter_are_a_422(client, logged_in, create, db, name):
    pid = create(1)["id"]
    before = _snapshot(db)
    r = post(client, logged_in, PATIENTS, person(2, father_name=name))
    assert r.status_code == 422
    assert "must contain at least one letter" in r.text
    assert patch(client, logged_in, pid, {"first_name": name}).status_code == 422
    assert _snapshot(db) == before


@pytest.mark.parametrize("name", ["李小龍", "محمد", "Nguyễn Văn An", "देवेन्द्र", "ʻOkalani"])
def test_international_names_are_accepted(client, logged_in, name):
    r = post(client, logged_in, PATIENTS, person(3, first_name=name))
    assert r.status_code == 201 and r.json()["item"]["first_name"] == name


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t", " \n\t \n "])
def test_notes_with_only_permitted_whitespace_are_stored_as_null(
    client, logged_in, create, db, blank
):
    r = post(client, logged_in, PATIENTS, person(4, notes=blank))
    assert r.status_code == 201 and r.json()["item"]["notes"] is None
    new_id = r.json()["item"]["id"]
    assert db(lambda s: s.get(Patient, new_id).notes) is None
    pid = create(5, notes="κάτι")["id"]
    r = patch(client, logged_in, pid, {"notes": blank})
    assert r.status_code == 200 and r.json()["item"]["notes"] is None
    assert db(lambda s: s.get(Patient, pid).notes) is None


def test_concurrent_creates_with_the_same_amka_give_one_201_and_one_409(app, client, logged_in):
    """Two requests race for one AMKA: BEGIN IMMEDIATE serialises them, so exactly one wins and
    the other gets a safe 409 — never a 500, never two rows. Same harness as the M5.1 race test
    (threads + barrier, five rounds)."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    cookies = dict(client.cookies)
    headers = unsafe_headers(logged_in)

    def attempt(body, barrier):
        racer = local_client(app, cookies=cookies, raise_server_exceptions=False)
        barrier.wait(timeout=15)
        response = racer.post(PATIENTS, json=body, headers=headers)
        return response.status_code, response.json(), response.text

    for round_no in range(5):
        shared = synthetic_amka(500 + round_no)
        bodies = [person(0, amka=shared, first_name="Πρώτος"), person(0, amka=shared)]
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda b, bar=barrier: attempt(b, bar), bodies))
        codes = sorted(status for status, _, _ in results)
        assert codes == [201, 409], (round_no, results)
        conflict = next(body for status, body, _ in results if status == 409)
        assert conflict["code"] == "duplicate_amka"
        assert all(shared not in raw for status, _, raw in results if status == 409)
        r = post(client, logged_in, f"{PATIENTS}/search", {"amka": shared})
        assert r.json()["total"] == 1
