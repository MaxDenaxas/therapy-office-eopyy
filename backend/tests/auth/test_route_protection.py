"""Structural route protection.

Every route of the real application — under ``/api`` or anywhere else — is enumerated
through FastAPI's *effective* route contexts (which merge router-level and include-level
dependencies), and its dependency tree is walked recursively. A route must require the
administrator session unless it is on the explicit, narrow allow-list:

  * ``PUBLIC_API_ROUTES``  health, login, session status (exact method + path)
  * ``DOCS_ROUTES``        the development-only documentation pages
  * the static frontend mount named ``frontend`` (the built shell; it carries no data)

The application is HTTP-only: a route without HTTP methods (a WebSocket route) is reported
unless it is in ``NON_HTTP_ROUTES``, which is empty on purpose. WebSocket scopes bypass the
HTTP middleware (loopback, Host and Origin checks), so supporting one needs those checks
rebuilt as pure ASGI first (docs/current-status.md).

Every protected route with an unsafe method must also require the CSRF check. The checker
is itself tested against deliberately unprotected routes, with and without the ``/api``
prefix, and against a second static mount.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.auth.dependencies import protected_router, require_admin, require_csrf
from app.config import Settings
from app.main import DOCS_ROUTES, PUBLIC_API_ROUTES, create_app
from tests.local_http import local_client

SAFE = {"GET", "HEAD"}


def calls(dependant: Dependant | None) -> Iterator[object]:
    if dependant is None:
        return
    for sub in dependant.dependencies:
        yield sub.call
        yield from calls(sub)


FRONTEND_MOUNT = "frontend"
NON_HTTP_ROUTES: frozenset[str] = frozenset()  # paths of WebSocket routes that are allowed


def is_frontend_mount(route) -> bool:
    """Only the built frontend: a *static-file* app mounted at the root, named ``frontend``.
    A sub-application (FastAPI or Starlette) is never exempt, whatever it is called or where it
    is mounted: its routes are not visible to this checker and could be unprotected."""
    original = route.original_route
    return (
        isinstance(original, Mount)
        and original.name == FRONTEND_MOUNT
        and original.path in ("", "/")  # Starlette stores the root mount as ""
        and isinstance(original.app, StaticFiles)
    )


def violations(app: FastAPI) -> list[str]:
    problems = []
    for route in iter_route_contexts(app.routes):
        path = route.path or ""
        methods = set(route.methods or ())
        dependant = getattr(route, "dependant", None)
        if dependant is None:  # not a FastAPI route: only the narrow exemptions are allowed
            if is_frontend_mount(route) or path in DOCS_ROUTES:
                continue
            problems.append(f"{sorted(methods) or 'ANY'} {path or '/'}: unprotected non-API route")
            continue
        if not methods:  # a WebSocket route: it has dependencies but no HTTP method
            if path not in NON_HTTP_ROUTES:
                problems.append(f"WEBSOCKET {path}: route without HTTP methods (HTTP-only app)")
            continue
        found = set(calls(dependant))
        for method in methods:
            if (method, path) in PUBLIC_API_ROUTES:
                continue
            if require_admin not in found:
                problems.append(f"{method} {path}: no administrator session required")
            if method not in SAFE and require_csrf not in found:
                problems.append(f"{method} {path}: no CSRF check")
    return problems


@pytest.fixture()
def real_app(tmp_path):
    return create_app(Settings(data_dir=tmp_path))


def test_every_api_route_is_protected_or_explicitly_public(real_app):
    assert violations(real_app) == []


def test_the_public_allow_list_is_minimal_and_exact(real_app):
    exposed = {
        (method, route.path)
        for route in iter_route_contexts(real_app.routes)
        if getattr(route, "dependant", None) is not None
        for method in route.methods or ()
        if require_admin not in set(calls(route.dependant))
    }
    assert (
        exposed
        == PUBLIC_API_ROUTES
        == {
            ("GET", "/api/health"),
            ("POST", "/api/auth/login"),
            ("GET", "/api/auth/session"),
        }
    )


def test_logout_is_protected_through_the_router_not_the_endpoint(real_app):
    (route,) = [r for r in iter_route_contexts(real_app.routes) if r.path == "/api/auth/logout"]
    found = set(calls(route.dependant))
    assert {require_admin, require_csrf} <= found


# ---------------------------------------------------------------- the checker catches gaps
@pytest.fixture()
def real_app_with_frontend(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    return create_app(Settings(data_dir=tmp_path / "data", frontend_dist=dist))


def test_the_checker_sees_every_route_including_non_api_ones(real_app_with_frontend):
    seen = {route.path or "" for route in iter_route_contexts(real_app_with_frontend.routes)}
    assert "/api/health" in seen and "/api/auth/logout" in seen
    assert any(not p.startswith("/api") for p in seen)  # the frontend mount is enumerated too


def test_the_frontend_mount_and_dev_docs_are_the_only_unprotected_non_api_routes(
    real_app_with_frontend,
):
    assert violations(real_app_with_frontend) == []
    mounts = [r for r in iter_route_contexts(real_app_with_frontend.routes) if is_frontend_mount(r)]
    assert len(mounts) == 1


def test_checker_detects_a_subapplication_mounted_under_the_frontend_name(real_app):
    sub = FastAPI()

    @sub.get("/patients")
    def leak():
        return ["unprotected"]

    real_app.mount("/admin", sub, name="frontend")
    problems = violations(real_app)
    assert len(problems) == 1 and "/admin" in problems[0]


def test_checker_detects_a_subapplication_mounted_at_the_root_under_the_frontend_name(real_app):
    sub = FastAPI()

    @sub.get("/patients")
    def leak():
        return ["unprotected"]

    real_app.mount("/", sub, name="frontend")
    assert len(violations(real_app)) == 1


def bare_app() -> FastAPI:
    """No default docs routes, so only what a test adds is checked."""
    return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def test_checker_detects_the_frontend_static_files_at_a_non_root_path(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    app = bare_app()
    app.mount("/static", StaticFiles(directory=dist), name=FRONTEND_MOUNT)  # right name, not root
    problems = violations(app)
    assert len(problems) == 1 and "/static" in problems[0]


def test_checker_detects_a_static_mount_at_the_root_with_another_name(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    app = bare_app()
    app.mount("/", StaticFiles(directory=dist), name="site")
    assert len(violations(app)) == 1


def test_checker_accepts_exactly_the_root_static_frontend_mount(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    app = bare_app()
    app.mount("/", StaticFiles(directory=dist), name=FRONTEND_MOUNT)
    assert violations(app) == []


def test_checker_detects_a_second_static_mount(real_app_with_frontend, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    real_app_with_frontend.mount("/files", StaticFiles(directory=extra), name="files")
    assert len(violations(real_app_with_frontend)) == 1
    assert "/files" in violations(real_app_with_frontend)[0]


def test_checker_detects_an_unprotected_route_outside_the_api_prefix(real_app):
    @real_app.get("/patients")
    def leak():
        return []

    @real_app.post("/internal/export")
    def export():
        return {}

    problems = violations(real_app)
    assert "GET /patients: no administrator session required" in problems
    assert "POST /internal/export: no administrator session required" in problems
    assert "POST /internal/export: no CSRF check" in problems
    assert len(problems) == 3


def test_checker_detects_an_unprotected_websocket_route(real_app):
    @real_app.websocket("/ws/patients")
    async def patients(websocket: WebSocket):
        await websocket.accept()

    assert violations(real_app) == [
        "WEBSOCKET /ws/patients: route without HTTP methods (HTTP-only app)"
    ]


def test_checker_detects_a_websocket_even_if_it_requires_the_session(real_app):
    """HTTP-only: a WebSocket is refused outright, protected or not, because WebSocket scopes
    bypass the HTTP security middleware."""
    router = APIRouter(dependencies=[Depends(require_csrf)])

    @router.websocket("/ws/secure")
    async def secure(websocket: WebSocket):
        await websocket.accept()

    real_app.include_router(router, prefix="/api")
    assert violations(real_app) == [
        "WEBSOCKET /api/ws/secure: route without HTTP methods (HTTP-only app)"
    ]


def test_checker_detects_a_plain_starlette_websocket_route(real_app):
    async def endpoint(websocket):
        await websocket.accept()

    real_app.router.add_websocket_route("/raw-ws", endpoint)
    problems = violations(real_app)
    assert len(problems) == 1 and "/raw-ws" in problems[0]


def test_the_websocket_allow_list_is_empty():
    assert NON_HTTP_ROUTES == frozenset()


def test_checker_accepts_a_protected_route_outside_the_api_prefix(real_app):
    router = protected_router()

    @router.get("/patients")
    def listing():
        return []

    real_app.include_router(router)
    assert violations(real_app) == []


def test_checker_detects_an_unprotected_route_added_to_the_app(real_app):
    @real_app.get("/api/patients")
    def leak():
        return []

    assert violations(real_app) == ["GET /api/patients: no administrator session required"]


def test_checker_detects_a_plain_router_included_with_a_prefix(real_app):
    inner = APIRouter()

    @inner.post("/prescriptions")
    def create():
        return {}

    outer = APIRouter(prefix="/v2")
    outer.include_router(inner)
    real_app.include_router(outer, prefix="/api")
    assert violations(real_app) == [
        "POST /api/v2/prescriptions: no administrator session required",
        "POST /api/v2/prescriptions: no CSRF check",
    ]


def test_checker_detects_a_session_check_without_csrf(real_app):
    router = APIRouter(dependencies=[Depends(require_admin)])

    @router.delete("/therapists/{tid}")
    def remove(tid: int):
        return {}

    real_app.include_router(router, prefix="/api")
    assert violations(real_app) == ["DELETE /api/therapists/{tid}: no CSRF check"]


def test_checker_accepts_routes_on_a_protected_router_nested_anywhere(real_app):
    protected = protected_router(prefix="/templates")

    @protected.get("")
    def listing():
        return []

    @protected.post("/{tid}/activate")
    def activate(tid: int):
        return {}

    wrapper = APIRouter(prefix="/library")
    wrapper.include_router(protected)
    real_app.include_router(wrapper, prefix="/api")
    assert violations(real_app) == []


def test_checker_finds_dependencies_declared_deep_in_the_tree(real_app):
    def wrapper(_ctx=Depends(require_csrf)):
        return None

    router = APIRouter(dependencies=[Depends(wrapper)])

    @router.put("/settings")
    def update():
        return {}

    real_app.include_router(router, prefix="/api")
    assert violations(real_app) == []


# ---------------------------------------------------------------- behaviour matches structure
def test_every_protected_route_refuses_an_anonymous_request(real_app):
    with local_client(real_app) as c:
        for route in iter_route_contexts(real_app.routes):
            if getattr(route, "dependant", None) is None:
                continue
            for method in route.methods or ():
                if (method, route.path) in PUBLIC_API_ROUTES:
                    continue
                r = c.request(method, route.path, headers={"Origin": "http://127.0.0.1:8000"})
                assert r.status_code == 401, (method, route.path)
