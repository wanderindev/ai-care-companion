"""Calendar feed (DESIGN §6.3, §7).

- ``GET|HEAD /calendar/{token}.ics``: the secret per-user ICS feed. No cookie
  (calendar apps fetch it on their own); the token in the path is the only
  credential. Unknown, malformed and revoked tokens, and tokens of
  deactivated users, all get the same 404. The token never reaches a log:
  uvicorn's access log is redacted (``app/core/logging.py``), the edge vhost
  turns access logging off for ``/api/v1/calendar/``, and nothing here logs.
- ``GET /calendar/feed``: whether the signed-in user has an active feed.
- ``POST /calendar/feed``: (re)generate it — revokes the old URL and returns
  the new one, the only time it is shown (only the hash is stored).
- ``DELETE /calendar/feed``: turn it off.

Writes are audited without the token or its hash.
"""

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.user import User
from app.schemas.calendar import CalendarFeedCreated, CalendarFeedStatus
from app.services import calendar_feed as svc

router = APIRouter(prefix="/calendar", tags=["calendar"])

FEED_NOT_FOUND = "Calendario no encontrado."
# Calendar apps poll every few hours anyway; a short private cache is enough.
FEED_CACHE_SECONDS = 15 * 60


@router.get("/feed", response_model=CalendarFeedStatus)
def get_feed(
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> CalendarFeedStatus:
    feed = svc.active_feed(db, user.id)
    if feed is None:
        return CalendarFeedStatus(active=False)
    return CalendarFeedStatus(active=True, created_at=feed.created_at)


@router.post("/feed", response_model=CalendarFeedCreated, status_code=status.HTTP_201_CREATED)
def create_feed(
    response: Response,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
) -> CalendarFeedCreated:
    feed, token = svc.regenerate(db, user)
    db.commit()
    url = svc.feed_url(settings.app_base_url, token)
    response.headers["Cache-Control"] = "no-store"
    return CalendarFeedCreated(created_at=feed.created_at, url=url, webcal_url=svc.webcal_url(url))


@router.delete("/feed", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    svc.revoke(db, user.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.api_route("/{token}.ics", methods=["GET", "HEAD"], include_in_schema=False)
def ics_feed(
    token: str,
    request: Request,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if svc.feed_for_token(db, token) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=FEED_NOT_FOUND)
    body = svc.render_calendar(svc.feed_events(db), settings.app_base_url).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {
        "Cache-Control": f"private, max-age={FEED_CACHE_SECONDS}",
        "ETag": etag,
        "Content-Disposition": 'inline; filename="n16chat-citas.ics"',
        "X-Robots-Tag": "noindex",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=body, media_type="text/calendar; charset=utf-8", headers=headers)
