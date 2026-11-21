"""Read-only request sessions for unsafe methods (``ReadDbSession``, M5.2 decision D) and the
query-parameter guard (``allow_query``, decision T).

``ReadDbSession`` is safe only if nothing else in the route's dependency tree opens a write
session: a second, ordinary ``DbSession`` would take ``BEGIN IMMEDIATE`` on its first statement.
The structural test below enforces that for every route of the real application, and is itself
tested against a deliberately wrong route.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import iter_route_contexts
from sqlalchemy import text

from app.api.deps import DbSession, ReadDbSession, allow_query, get_db, get_read_db
from app.auth.dependencies import protected_router
from app.config import Settings
from app.main import create_app
from tests.auth.conftest import unsafe_headers
from tests.auth.test_route_protection import calls

READ_ONLY_ROUTES = {("POST", "/api/patients/search")}


def read_session_violations(app: FastAPI) -> list[str]:
    problems = []
    for route in iter_route_contexts(app.routes):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        found = set(calls(dependant))
        if get_read_db in found and get_db in found:
            problems.append(f"{sorted(route.methods or ())} {route.path}: read and write session")
    return problems


def routes_using(app: FastAPI, dependency) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in iter_route_contexts(app.routes)
        if getattr(route, "dependant", None) is not None
        and dependency in set(calls(route.dependant))
        for method in route.methods or ()
    }


@pytest.fixture()
def real_app(tmp_path):
    return create_app(Settings(data_dir=tmp_path))


def test_no_route_uses_a_read_session_together_with_a_write_session(real_app):
    assert read_session_violations(real_app) == []


def test_the_patient_search_uses_the_read_session_and_only_it(real_app):
    assert routes_using(real_app, get_read_db) == READ_ONLY_ROUTES
    assert not (routes_using(real_app, get_db) & READ_ONLY_ROUTES)


def test_the_checker_catches_a_route_that_also_opens_a_write_session(real_app):
    router = protected_router(prefix="/api/wrong")

    def helper(db: DbSession) -> None:  # hidden in a sub-dependency
        return None

    @router.post("/both")
    def both(db: ReadDbSession, _=Depends(helper)):
        return {}

    real_app.include_router(router)
    assert read_session_violations(real_app) == ["['POST'] /api/wrong/both: read and write session"]


# ---------------------------------------------------------------- behaviour
@pytest.fixture()
def read_routes(app):
    router = protected_router(prefix="/api/test-read")

    @router.post("/count")
    def count(db: ReadDbSession):
        return {"n": db.execute(text("SELECT count(*) FROM patients")).scalar()}

    @router.post("/write")
    def write(db: ReadDbSession):
        db.execute(
            text("INSERT INTO audit_log (action, entity, entity_id) VALUES ('x.test', 'test', 1)")
        )
        return {}

    app.include_router(router)


def test_a_read_session_reads(client, logged_in, read_routes):
    r = client.post("/api/test-read/count", json={}, headers=unsafe_headers(logged_in))
    assert r.status_code == 200 and r.json() == {"n": 0}


def test_a_write_through_a_read_session_fails_and_persists_nothing(
    client, logged_in, read_routes, db
):
    r = client.post("/api/test-read/write", json={}, headers=unsafe_headers(logged_in))
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    query = text("SELECT count(*) FROM audit_log WHERE entity = 'test'")
    assert db(lambda s: s.execute(query).scalar()) == 0


def test_a_read_session_route_still_needs_session_csrf_and_origin(client, admin, read_routes):
    path = "/api/test-read/count"
    r = client.post(path, json={}, headers=unsafe_headers())
    assert r.status_code == 401
    from tests.auth.conftest import login

    csrf = login(client).json()["csrf_token"]
    assert client.post(path, json={}, headers=unsafe_headers()).status_code == 403  # no CSRF
    r = client.post(path, json={}, headers={"X-CSRF-Token": csrf})  # no Origin
    assert r.status_code == 403 and r.json() == {"code": "origin_rejected"}
    assert client.post(path, json={}, headers=unsafe_headers(csrf)).status_code == 200


# ---------------------------------------------------------------- query-parameter guard
@pytest.fixture()
def guarded(app):
    router = protected_router(prefix="/api/test-query")

    @router.get("/items", dependencies=[Depends(allow_query("a", "b"))])
    def items(a: int | None = None, b: int | None = None):
        return {"a": a, "b": b}

    @router.get("/none", dependencies=[Depends(allow_query())])
    def none():
        return {}

    app.include_router(router)


@pytest.mark.parametrize(
    "query, status",
    [
        ("", 200),
        ("a=1", 200),
        ("a=1&b=2", 200),
        ("c=1", 422),
        ("a=1&c=1", 422),
        ("a=1&a=2", 422),  # repeated
        ("A=1", 422),  # names are exact
        ("secretname", 422),  # a bare key
    ],
)
def test_allow_query(client, logged_in, guarded, query, status):
    r = client.get(f"/api/test-query/items?{query}")
    assert r.status_code == status, query
    if status == 422:
        assert r.json() == {"code": "unknown_query_parameter"}  # never names the parameter


def test_allow_query_with_no_names_refuses_everything(client, logged_in, guarded):
    assert client.get("/api/test-query/none").status_code == 200
    assert client.get("/api/test-query/none?x=1").json() == {"code": "unknown_query_parameter"}


def test_the_guard_runs_after_authentication(client, admin, guarded):
    assert client.get("/api/test-query/none?x=1").status_code == 401
