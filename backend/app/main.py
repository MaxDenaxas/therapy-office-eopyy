"""Application factory.

Run (development):  uvicorn app.main:create_app --factory --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import health
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Therapy Office",
        version=__version__,
        # The interactive docs are a development aid only.
        docs_url="/api/docs" if settings.environment == "development" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.environment == "development" else None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(health.router, prefix="/api")

    # Serve the built frontend (one process, one port). Mounted last so /api wins.
    if settings.frontend_dist is not None:
        dist = settings.frontend_dist.resolve()
        if not dist.is_dir():
            raise RuntimeError(
                f"THERAPY_FRONTEND_DIST {dist} is not a directory; run the frontend build."
            )
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app
