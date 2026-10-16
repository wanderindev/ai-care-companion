from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    analysis_runs,
    appointments,
    attachments,
    auth,
    calendar,
    chat,
    doctor_inference,
    doctors,
    extension,
    health,
    jobs,
    labs,
    ledd,
    log_entries,
    me,
    meds,
    patients,
    suggestions,
    summaries,
    whatsapp_imports,
)
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging

settings = get_settings()

# Also redacts URL-borne secrets (calendar feed / magic-link tokens) from
# uvicorn's access log; see app/core/logging.py.
configure_logging(settings.log_level)

# The SPA and the API share one origin in production (the edge nginx routes
# /api/ to the backend) and the Vite dev server proxies /api locally, so the
# app itself needs no CORS. The only cross-origin caller is the WhatsApp Web
# sync extension (EXTENSION_ORIGINS, install_extension_cors below).
app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    debug=settings.debug,
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@app.middleware("http")
async def require_csrf_header(request: Request, call_next):
    """CSRF guard (DESIGN §8): state-changing requests need X-Requested-With.

    With SameSite=Lax cookies and no CORS for web origins (the extension's
    CORS never allows credentials), a cross-site form or fetch cannot add this
    header, so it cannot act with a family member's session. The SPA's api
    client (frontend/src/lib/api.js) always sends it on writes, and so does
    the sync extension.
    """
    if request.method not in SAFE_METHODS and not request.headers.get("x-requested-with"):
        return JSONResponse(status_code=403, content={"detail": "Solicitud no permitida."})
    return await call_next(request)


def install_extension_cors(target: FastAPI, config: Settings) -> bool:
    """CORS for the sync extension's origins only (``EXTENSION_ORIGINS``;
    empty = nothing installed). Never with credentials: the extension uses its
    bearer token, so a cross-origin request can never ride a family member's
    session cookie. Added after the CSRF middleware, so it is the outer one
    and answers preflights itself."""
    origins = list(config.extension_origin_list)
    if not origins:
        return False
    target.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
        expose_headers=["X-Error-Code"],
        max_age=600,
    )
    return True


install_extension_cors(app, settings)

app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(patients.router, prefix="/api/v1")
app.include_router(doctors.router, prefix="/api/v1")
app.include_router(doctor_inference.router, prefix="/api/v1")
app.include_router(attachments.router, prefix="/api/v1")
app.include_router(log_entries.router, prefix="/api/v1")
app.include_router(appointments.router, prefix="/api/v1")
app.include_router(calendar.router, prefix="/api/v1")
app.include_router(me.router, prefix="/api/v1")
app.include_router(meds.router, prefix="/api/v1")
app.include_router(ledd.router, prefix="/api/v1")
app.include_router(summaries.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(whatsapp_imports.router, prefix="/api/v1")
app.include_router(extension.router, prefix="/api/v1")
app.include_router(analysis_runs.router, prefix="/api/v1")
app.include_router(labs.reports_router, prefix="/api/v1")
app.include_router(labs.analytes_router, prefix="/api/v1")
app.include_router(labs.links_router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(suggestions.router, prefix="/api/v1")
