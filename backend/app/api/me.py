"""The signed-in user's own settings (DESIGN §6.3).

- ``GET /me/settings``: ``{email, email_invites}``.
- ``PATCH /me/settings``: ``{"email_invites": true|false}`` — «Recibir
  invitaciones por correo» (``app.services.calendar_invites``). Audited
  (entity ``user``) when the value changes.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.me import UserSettingsOut, UserSettingsUpdate
from app.services import audit

router = APIRouter(prefix="/me", tags=["me"])


def _out(user: User) -> UserSettingsOut:
    return UserSettingsOut(email=user.email, email_invites=user.email_invites)


@router.get("/settings", response_model=UserSettingsOut)
def get_settings_(user: User = Depends(require_user)) -> UserSettingsOut:
    return _out(user)


@router.patch("/settings", response_model=UserSettingsOut)
def patch_settings(
    payload: UserSettingsUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> UserSettingsOut:
    row = db.get(User, user.id, with_for_update=True)
    if row.email_invites != payload.email_invites:
        before = {"email_invites": row.email_invites}
        row.email_invites = payload.email_invites
        db.flush()
        audit.record(
            db,
            user_id=user.id,
            action=audit.ACTION_UPDATE,
            entity="user",
            entity_id=user.id,
            before=before,
            after={"email_invites": row.email_invites},
        )
    db.commit()
    return _out(row)
