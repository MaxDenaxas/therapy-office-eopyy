"""Privacy acceptance test for M5.2: the full AMKA never appears in lists, search results, audit
entries, error bodies or logs; names never appear in audit entries, error bodies or logs.

Sentinel values are synthetic (``synthetic_amka``) and distinctive, so any leak is detectable."""

from __future__ import annotations

import json
import logging

from sqlalchemy import select

from app.models import AuditLog
from tests.patients.amka import synthetic_amka
from tests.patients.conftest import PATIENTS, unsafe_headers

AMKA_A = synthetic_amka(987654321)
AMKA_B = synthetic_amka(876543219)
AMKA_C = synthetic_amka(765432198)
NAMES = ("Ξενοφώνταςμυστικό", "Ζαχαροπλαστάκηςκρυφό", "Θεμιστοκλήςαπόρρητο")


def walk(value):
    """Every key and value of a JSON document, as strings."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)
    else:
        yield str(value)


def test_the_full_amka_and_names_never_leak(client, logged_in, db, caplog):
    caplog.set_level(logging.DEBUG)
    h = unsafe_headers(logged_in)
    first, last, father = NAMES
    body = {"first_name": first, "last_name": last, "father_name": father, "amka": AMKA_A}

    lists, errors = [], []
    r = client.post(PATIENTS, json=body, headers=h)
    assert r.status_code == 201
    pid = r.json()["item"]["id"]

    # failing paths: every error body is collected
    errors.append(client.post(PATIENTS, json=body, headers=h))  # 409 duplicate
    errors.append(client.post(PATIENTS, json={**body, "amka": AMKA_A + "9"}, headers=h))  # 422
    errors.append(client.post(PATIENTS, json={**body, "first_name": first + "\t"}, headers=h))
    errors.append(client.post(PATIENTS, json={**body, "unknown": AMKA_B}, headers=h))
    errors.append(client.post(PATIENTS, json={**body, AMKA_B: 1}, headers=h))  # as a key
    errors.append(client.patch(f"{PATIENTS}/{pid}", json={"amka": AMKA_B}, headers=h))
    errors.append(client.patch(f"{PATIENTS}/{pid}", json={"last_name": "α" * 101}, headers=h))
    errors.append(client.post(f"{PATIENTS}/{pid}/amka", json={"amka": AMKA_B[:-1]}, headers=h))
    errors.append(client.post(f"{PATIENTS}/search", json={"amka": AMKA_B + "1"}, headers=h))
    errors.append(
        client.post(f"{PATIENTS}/search", json={"query": first, "amka": AMKA_A}, headers=h)
    )
    errors.append(client.post(f"{PATIENTS}/search", json={"query": "%%"}, headers=h))
    errors.append(client.get(f"{PATIENTS}?amka={AMKA_A}"))  # refused, never echoed
    errors.append(client.get(f"{PATIENTS}?{AMKA_A}=1"))
    errors.append(client.get(f"{PATIENTS}?q={first}"))
    errors.append(client.get(f"{PATIENTS}/{AMKA_A}"))  # an AMKA-shaped id: 404
    errors.append(client.get(f"{PATIENTS}/999999"))
    assert all(400 <= e.status_code < 500 for e in errors), [e.status_code for e in errors]

    # a second patient, then corrections and lifecycle
    r = client.post(PATIENTS, json={**body, "amka": AMKA_C, "first_name": "Άλλος"}, headers=h)
    assert r.status_code == 201
    errors.append(client.post(f"{PATIENTS}/{pid}/amka", json={"amka": AMKA_C}, headers=h))  # 409
    assert (
        client.patch(f"{PATIENTS}/{pid}", json={"father_name": father + "β"}, headers=h).status_code
        == 200
    )
    assert (
        client.post(f"{PATIENTS}/{pid}/amka", json={"amka": AMKA_B}, headers=h).status_code == 200
    )
    assert client.post(f"{PATIENTS}/{pid}/deactivate", headers=h).status_code == 200
    assert client.post(f"{PATIENTS}/{pid}/activate", headers=h).status_code == 200

    # lists and every kind of search
    lists.append(client.get(PATIENTS))
    lists.append(client.get(f"{PATIENTS}?active=true&limit=5"))
    lists.append(client.post(f"{PATIENTS}/search", json={"query": first[:5]}, headers=h))
    lists.append(client.post(f"{PATIENTS}/search", json={"amka": AMKA_B}, headers=h))
    lists.append(client.post(f"{PATIENTS}/search", json={"amka": AMKA_C}, headers=h))
    assert all(r.status_code == 200 for r in lists)
    assert sum(r.json()["total"] for r in lists) >= 6

    for response in lists:
        values = list(walk(response.json()))
        for amka in (AMKA_A, AMKA_B, AMKA_C):
            assert amka not in response.text
            assert not any(amka in v for v in values)
        assert all("amka" != k for item in response.json()["items"] for k in item)

    for response in errors:
        for secret in (AMKA_A, AMKA_B, AMKA_C, *NAMES):
            assert secret not in response.text, (response.status_code, response.text)

    audit = db(
        lambda s: json.dumps(
            [
                (r.action, r.entity, r.entity_id, r.details)
                for r in s.scalars(select(AuditLog).order_by(AuditLog.id))
            ],
            ensure_ascii=False,
        )
    )
    # the application's log records (the test client's own ``httpx2`` logger records the URLs
    # this test deliberately sends; it is a test-only dependency, not application output)
    app_logs = " | ".join(
        f"{r.name} {r.getMessage()} {r.exc_text or ''}"
        for r in caplog.records
        if not r.name.startswith(("httpx", "httpcore"))
    )
    assert any(r.name.startswith("httpx") for r in caplog.records)  # capture was working
    for secret in (AMKA_A, AMKA_B, AMKA_C, *NAMES):
        assert secret not in audit
        assert secret not in app_logs

    # the detail view is the one place the full AMKA appears
    detail = client.get(f"{PATIENTS}/{pid}")
    assert detail.json()["item"]["amka"] == AMKA_B
