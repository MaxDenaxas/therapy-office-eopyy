from fastapi import APIRouter, Depends

from app import __version__
from app.config import Settings, get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Liveness check. Deliberately exposes no paths, patient data or configuration details."""
    return {"status": "ok", "version": __version__, "environment": settings.environment}
