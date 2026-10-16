"""The WhatsApp Web sync extension's token (DESIGN §6.8, docs/extension-api.md).

Import admins only (``require_importer``: admin + ``users.can_import``), and
only with the session cookie (a token cannot mint tokens):

- ``GET /extension/token``: whether the signed-in admin has an active token;
- ``POST /extension/token``: (re)generate it — revokes the old one and
  returns the new token, the only time it is shown (only the hash is stored);
- ``DELETE /extension/token``: revoke it.

Writes are audited without the token or its hash.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_importer
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.user import User
from app.schemas.extension import ExtensionTokenCreated, ExtensionTokenStatus
from app.services import extension_tokens as svc

router = APIRouter(prefix="/extension", tags=["extension"])


@router.get("/token", response_model=ExtensionTokenStatus)
def get_token(
    db: DbSession = Depends(get_db), user: User = Depends(require_importer)
) -> ExtensionTokenStatus:
    row = svc.active_token(db, user.id)
    if row is None:
        return ExtensionTokenStatus(active=False)
    return ExtensionTokenStatus(
        active=True, created_at=row.created_at, last_used_at=row.last_used_at
    )


@router.post("/token", response_model=ExtensionTokenCreated, status_code=status.HTTP_201_CREATED)
def create_token(
    response: Response,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_importer),
) -> ExtensionTokenCreated:
    row, token = svc.regenerate(db, user)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return ExtensionTokenCreated(
        created_at=row.created_at,
        token=token,
        api_base=settings.app_base_url.rstrip("/") + "/api/v1",
    )


@router.delete("/token", status_code=status.HTTP_204_NO_CONTENT)
def delete_token(
    db: DbSession = Depends(get_db), user: User = Depends(require_importer)
) -> Response:
    svc.revoke(db, user.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
