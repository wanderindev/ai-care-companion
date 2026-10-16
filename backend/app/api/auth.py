import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session as DbSession

from app.api.deps import clear_session_cookie, client_ip, require_user, set_session_cookie
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import RequestLinkIn, RequestLinkOut, UserOut, VerifyIn
from app.services import auth as auth_service

logger = logging.getLogger("app.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Same answer whether or not the address is on the allowlist (no enumeration)
# and whether or not a rate limit was hit.
LINK_SENT_MESSAGE = (
    "Si tu correo está autorizado, recibirás un enlace para entrar. Revisa tu correo."
)
INVALID_LINK_MESSAGE = "El enlace no es válido o ya venció. Pide uno nuevo."


@router.post("/request-link", response_model=RequestLinkOut)
def request_link(
    payload: RequestLinkIn,
    request: Request,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RequestLinkOut:
    try:
        user, token = auth_service.create_magic_link(db, payload.email, client_ip(request))
    except auth_service.RateLimited:
        # Same 200 as always: a visible 429 would lock a family member out with
        # an error and hint at limits. Nothing is written or sent.
        logger.warning("Magic-link request over the rate limit; ignored")
        return RequestLinkOut(message=LINK_SENT_MESSAGE)
    if user is not None:
        background.add_task(
            auth_service.send_magic_link_email,
            settings,
            user.email,
            user.display_name,
            auth_service.magic_link_url(settings, token),
        )
    return RequestLinkOut(message=LINK_SENT_MESSAGE)


@router.post("/verify", response_model=UserOut)
def verify(
    payload: VerifyIn,
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    user = auth_service.consume_magic_link(db, payload.token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_LINK_MESSAGE)
    _, session_token = auth_service.create_session(
        db, settings, user, request.headers.get("user-agent")
    )
    set_session_cookie(response, settings, session_token)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        auth_service.delete_session(db, token)
    clear_session_cookie(response, settings)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(require_user)) -> User:
    return user
