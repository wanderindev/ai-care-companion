"""Sync-extension API tokens (DESIGN §6.8, docs/extension-api.md).

Why a token and not the session cookie: the session cookie is HttpOnly and
SameSite=Lax and must stay that way for browsers; letting an extension reuse
it would mean reading the cookie through ``chrome.cookies`` and sending it
cross-origin. A per-admin bearer token generated in the app is simpler, is
revocable on its own and opens only what the extension needs.

Tokens mirror the calendar feed (``calendar_feed``): ``n16x_`` + 256 random
bits URL-safe, and only the SHA-256 of the whole string is stored (looked up
by hash through a unique index). One active token per user; regenerating
revokes the previous one. The token is accepted only by
``app.api.deps.require_import_admin`` (the WhatsApp import endpoints), and
only while its user is active and an import admin (admin + ``can_import``).
Audit rows never hold the token or its hash; nothing here logs it.
"""

import re
from datetime import UTC, datetime, timedelta

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.extension_token import ExtensionToken
from app.models.user import User
from app.services import audit
from app.services.auth import hash_token, new_token

ENTITY = "extension_token"
PREFIX = "n16x_"
LAST_USED_INTERVAL = timedelta(hours=1)
_TOKEN_RE = re.compile(r"^n16x_[A-Za-z0-9_-]{32,128}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def active_token(db: DbSession, user_id: int) -> ExtensionToken | None:
    return db.scalar(
        select(ExtensionToken).where(
            ExtensionToken.user_id == user_id, ExtensionToken.revoked_at.is_(None)
        )
    )


def _audit_view(row: ExtensionToken) -> dict:
    # Never the token or its hash.
    return jsonable_encoder(
        {"user_id": row.user_id, "created_at": row.created_at, "revoked_at": row.revoked_at}
    )


def revoke(db: DbSession, user_id: int) -> ExtensionToken | None:
    """Revoke the user's active token (audited); the caller commits."""
    row = active_token(db, user_id)
    if row is None:
        return None
    before = _audit_view(row)
    row.revoked_at = utcnow()
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity=ENTITY,
        entity_id=row.id,
        before=before,
        after=_audit_view(row),
    )
    return row


def regenerate(db: DbSession, user: User) -> tuple[ExtensionToken, str]:
    """Revoke the active token (if any) and create a new one; returns
    ``(row, token)``. The caller commits. The user row is locked so two
    concurrent requests cannot both create an active token."""
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    revoke(db, user.id)
    token = PREFIX + new_token()
    row = ExtensionToken(user_id=user.id, token_hash=hash_token(token))
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_CREATE,
        entity=ENTITY,
        entity_id=row.id,
        after=_audit_view(row),
    )
    return row, token


def user_for_token(db: DbSession, token: str) -> User | None:
    """The active user owning the active ``token``, else None. Refreshes
    ``last_used_at`` (committing) at most once per ``LAST_USED_INTERVAL``."""
    if not _TOKEN_RE.match(token):
        return None
    row = db.scalar(
        select(ExtensionToken)
        .join(User, User.id == ExtensionToken.user_id)
        .where(
            ExtensionToken.token_hash == hash_token(token),
            ExtensionToken.revoked_at.is_(None),
            User.is_active.is_(True),
        )
    )
    if row is None:
        return None
    now = utcnow()
    if row.last_used_at is None or now - row.last_used_at >= LAST_USED_INTERVAL:
        row.last_used_at = now
        db.commit()
    return row.user
