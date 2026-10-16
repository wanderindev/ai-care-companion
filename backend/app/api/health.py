from fastapi import APIRouter

from app.core.config import get_settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check for local dev, CI and deploy verification. No auth, no DB."""
    settings = get_settings()
    return HealthResponse(status="healthy", service=settings.app_name, version=settings.version)
