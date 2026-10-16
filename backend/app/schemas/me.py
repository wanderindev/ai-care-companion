from pydantic import BaseModel, ConfigDict


class UserSettingsOut(BaseModel):
    """``GET/PATCH /me/settings``: the signed-in user's own preferences."""

    email: str
    email_invites: bool


class UserSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_invites: bool
