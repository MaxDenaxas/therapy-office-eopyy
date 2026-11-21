"""Application factory.

Run (development):  python -m app.cli serve --reload
                    (or: uvicorn app.main:create_app --factory --reload --host 127.0.0.1
                     --no-access-log — the access log would record query strings; never use
                     --log-level trace, which logs whole request scopes including cookies)

Routing: public routes are listed explicitly (``PUBLIC_API_ROUTES``); every other
API router is created with ``protected_router()`` and therefore requires the
administrator session and CSRF token. A structural test enforces this.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import auth, health, patients, reference
from app.api.deps import Database, utc_now
from app.config import Settings, get_settings
from app.errors import install_crash_guard, install_error_handlers
from app.security import install_security

# (method, path) pairs reachable without a session. Nothing else under /api.
PUBLIC_API_ROUTES = frozenset(
    {
        ("GET", "/api/health"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/session"),
    }
)
# development-only documentation (absent in production)
DOCS_ROUTES = frozenset({"/api/docs", "/api/docs/oauth2-redirect", "/api/openapi.json"})


def create_app(
    settings: Settings | None = None, clock: Callable[[], datetime] | None = None
) -> FastAPI:
    settings = settings or get_settings()
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        database.dispose()

    app = FastAPI(
        title="Therapy Office",
        version=__version__,
        # The interactive docs are a development aid only.
        docs_url="/api/docs" if settings.environment == "development" else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
        openapi_url="/api/openapi.json" if settings.environment == "development" else None,
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.clock = clock or utc_now
    app.dependency_overrides[get_settings] = lambda: settings
    install_error_handlers(app)
    install_security(app)
    install_crash_guard(app)  # outermost: nothing unexpected reaches the server's log

    app.include_router(health.router, prefix="/api")
    app.include_router(auth.public, prefix="/api")
    app.include_router(auth.protected, prefix="/api")
    app.include_router(reference.therapy_types, prefix="/api")
    app.include_router(reference.therapists, prefix="/api")
    app.include_router(patients.patients, prefix="/api")

    # Serve the built frontend (one process, one port). Mounted last so /api wins.
    if settings.frontend_dist is not None:
        dist = settings.frontend_dist.resolve()
        if not dist.is_dir():
            raise RuntimeError(
                f"THERAPY_FRONTEND_DIST {dist} is not a directory; run the frontend build."
            )
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app
