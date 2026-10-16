"""Auth dependencies for routers: ``require_user``, ``require_admin``,
``require_importer`` (an admin with ``users.can_import``, by cookie) and
``require_import_admin`` (the same, by cookie or by the sync extension's
token)."""

import ipaddress

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.user import User
from app.services import auth as auth_service
from app.services import extension_tokens

NOT_AUTHENTICATED = "No has iniciado sesión."


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str | None:
    """The client IP for rate limiting, from a value the edge controls.

    The edge nginx (pic-frontend) sets ``X-Real-IP $remote_addr`` on the
    ``/api/`` location, which *overwrites* anything the client sent. Do not
    use ``request.client.host`` behind the edge: uvicorn runs with
    ``--forwarded-allow-ips *`` and takes the leftmost ``X-Forwarded-For``
    entry, which the client chooses. Without X-Real-IP (local dev, tests) fall
    back to the socket peer. Anything that is not a valid IP is dropped, so the
    stored value always fits ``requested_ip`` (45 chars).
    """
    real_ip = _valid_ip(request.headers.get("x-real-ip"))
    if real_ip:
        return real_ip
    return _valid_ip(request.client.host if request.client else None)


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_days * 24 * 3600,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def require_user(
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """The signed-in, active user; 401 otherwise. Slides the session expiry."""
    token = request.cookies.get(settings.session_cookie_name)
    session = auth_service.get_valid_session(db, token) if token else None
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=NOT_AUTHENTICATED)
    if auth_service.refresh_session_if_due(db, settings, session):
        set_session_cookie(response, settings, token)
    return session.user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Solo administradores.")
    return user


NOT_IMPORTER = "Solo quien administra las importaciones."


def _check_importer(user: User) -> User:
    if not user.is_import_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_IMPORTER)
    return user


def require_importer(user: User = Depends(require_user)) -> User:
    """An admin with the import permission (``User.is_import_admin``), signed
    in with the session cookie. Guards ``/analysis-runs`` and
    ``/extension/token``; narrower than ``require_admin`` (ticket #96)."""
    return _check_importer(user)


INVALID_EXTENSION_TOKEN = "La clave de la extensión no es válida o fue revocada."


def require_import_admin(
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """An admin with the import permission (``require_importer``), signed in
    with the session cookie **or** with the sync extension's bearer token
    (``app.services.extension_tokens``). A token whose owner lost the
    permission (or the admin role) is refused with 403.

    Only the WhatsApp import endpoints use this. A request that carries an
    ``Authorization`` header is judged by that header alone (its cookies are
    ignored); without the header it is ``require_importer``. The CSRF middleware
    still applies to both: the extension sends ``X-Requested-With`` on writes.
    """
    header = request.headers.get("authorization")
    if header is None:
        return _check_importer(require_user(request, response, db, settings))
    scheme, _, token = header.partition(" ")
    user = None
    if scheme.lower() == "bearer" and token.strip():
        user = extension_tokens.user_for_token(db, token.strip())
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_EXTENSION_TOKEN,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _check_importer(user)
