from datetime import datetime

from pydantic import BaseModel


class CalendarFeedStatus(BaseModel):
    """``GET /calendar/feed``: the URL itself is never shown again (only its
    hash is stored)."""

    active: bool
    created_at: datetime | None = None


class CalendarFeedCreated(BaseModel):
    """``POST /calendar/feed``: the only response that carries the URL."""

    active: bool = True
    created_at: datetime
    url: str
    webcal_url: str
