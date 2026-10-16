from datetime import datetime

from pydantic import BaseModel


class ExtensionTokenStatus(BaseModel):
    """``GET /extension/token``: the token itself is never shown again (only
    its hash is stored)."""

    active: bool
    created_at: datetime | None = None
    last_used_at: datetime | None = None


class ExtensionTokenCreated(BaseModel):
    """``POST /extension/token``: the only response that carries the token."""

    active: bool = True
    created_at: datetime
    token: str
    # The app's origin, for the extension's settings (it calls f"{api_base}/…").
    api_base: str
