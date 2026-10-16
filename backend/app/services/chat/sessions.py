"""Archive, restore and delete chat sessions (#99, DESIGN §5.7).

Sessions are shared, so any member may archive or restore one; deleting is a
hard delete reserved to admins and to the member who started the
conversation (``can_delete``). Both refuse while a turn is generating: the
turn's background thread would otherwise write into a hidden or vanished
session. The check runs with the session row locked (``FOR UPDATE``), and
``engine.acquire_lock`` is an ``UPDATE`` of that row, so a turn cannot start
in between.

Delete: messages go with the session (``ON DELETE CASCADE``); suggestions the
chat proposed keep working with ``chat_session_id`` NULL (``ON DELETE SET
NULL``). Files the answers generated (``owner_type='chat_message'``) are
unlinked when another attachment row shares their stored object (e.g. a copy
made when a suggestion was approved); a file whose object nothing else uses
loses its row, and the object is removed from storage **after** the commit
(:func:`purge_objects`). The audit row keeps ids and counts only — never the
title or message text.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.attachment import Attachment
from app.models.chat import ChatMessage, ChatSession
from app.models.suggestion import Suggestion
from app.models.user import User
from app.services import audit
from app.services.chat import engine
from app.services.storage import Storage

logger = logging.getLogger(__name__)

OWNER_TYPE = "chat_message"


class SessionNotFound(LookupError):
    pass


class SessionBusy(RuntimeError):
    pass


class DeleteForbidden(PermissionError):
    pass


def can_delete(session: ChatSession, user: User) -> bool:
    """Admins, and the member who started the conversation."""
    return user.is_admin or (session.created_by is not None and session.created_by == user.id)


def _locked(db: DbSession, session_id: int) -> ChatSession:
    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id).with_for_update())
    if session is None:
        raise SessionNotFound(session_id)
    if engine.is_generating(session):
        raise SessionBusy(session_id)
    return session


def set_archived(db: DbSession, session_id: int, archived: bool, *, user: User) -> ChatSession:
    """Archive (``archived_at`` = now) or restore (NULL) a session, audited.
    Already in that state: no change, no audit row. The caller commits."""
    session = _locked(db, session_id)
    if (session.archived_at is not None) == archived:
        return session
    before = audit.snapshot(session)
    session.archived_at = datetime.now(UTC) if archived else None
    session.updated_by = user.id
    db.flush()
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_UPDATE,
        entity="chat_session",
        entity_id=session.id,
        before={"archived_at": before.get("archived_at")},
        after={"archived_at": audit.snapshot(session).get("archived_at")},
    )
    return session


@dataclass
class DeleteResult:
    session_id: int
    # Stored objects no row refers to any more: remove them after the commit.
    orphan_keys: list[str] = field(default_factory=list)


def delete_session(db: DbSession, session_id: int, *, user: User) -> DeleteResult:
    """Hard-delete a session and its messages (see the module docstring).
    The caller commits, then calls :func:`purge_objects`."""
    session = _locked(db, session_id)
    if not can_delete(session, user):
        raise DeleteForbidden(session_id)

    message_ids = list(
        db.scalars(select(ChatMessage.id).where(ChatMessage.session_id == session_id))
    )
    files = (
        list(
            db.scalars(
                select(Attachment)
                .where(Attachment.owner_type == OWNER_TYPE, Attachment.owner_id.in_(message_ids))
                .order_by(Attachment.id)
                .with_for_update()
            )
        )
        if message_ids
        else []
    )
    suggestion_ids = list(
        db.scalars(
            select(Suggestion.id)
            .where(Suggestion.chat_session_id == session_id)
            .order_by(Suggestion.id)
        )
    )

    unlinked: list[int] = []
    deleted: list[int] = []
    orphan_keys: list[str] = []
    for att in files:
        shared = db.scalar(
            select(func.count())
            .select_from(Attachment)
            .where(Attachment.storage_key == att.storage_key, Attachment.id != att.id)
        )
        before = audit.snapshot(att)
        if shared:
            att.owner_type, att.owner_id = None, None
            unlinked.append(att.id)
            audit.record(
                db,
                user_id=user.id,
                action=audit.ACTION_UNLINK,
                entity="attachment",
                entity_id=att.id,
                before=before,
                after=audit.snapshot(att),
            )
        else:
            deleted.append(att.id)
            if att.storage_key not in orphan_keys:
                orphan_keys.append(att.storage_key)
            audit.record(
                db,
                user_id=user.id,
                action=audit.ACTION_DELETE,
                entity="attachment",
                entity_id=att.id,
                before=audit.snapshot(att, bookkeeping=True),
            )
            db.delete(att)

    summary = {
        "id": session.id,
        "created_by": session.created_by,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_message_at": (
            session.last_message_at.isoformat() if session.last_message_at else None
        ),
        "archived_at": session.archived_at.isoformat() if session.archived_at else None,
        "message_count": len(message_ids),
        "unlinked_attachment_ids": unlinked,
        "deleted_attachment_ids": deleted,
        "suggestion_ids": suggestion_ids,
    }
    db.delete(session)
    db.flush()
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_DELETE,
        entity="chat_session",
        entity_id=session_id,
        before=summary,
    )
    return DeleteResult(session_id=session_id, orphan_keys=orphan_keys)


def purge_objects(db: DbSession, keys: list[str], storage_factory: Callable[[], Storage]) -> int:
    """Remove stored objects of a committed delete, skipping any key a row
    uses again by now (an upload deduped onto it meanwhile). Best effort: a
    failure is logged (type only) and leaves the object behind. Returns how
    many objects were removed."""
    if not keys:
        return 0
    still_used = set(
        db.scalars(select(Attachment.storage_key).where(Attachment.storage_key.in_(keys)))
    )
    pending = [k for k in keys if k not in still_used]
    if not pending:
        return 0
    try:
        storage = storage_factory()
    except Exception as exc:
        logger.warning("chat: delete left %d stored files (%s)", len(pending), type(exc).__name__)
        return 0
    removed = 0
    for key in pending:
        try:
            storage.delete(key)
            removed += 1
        except Exception as exc:
            logger.warning("chat: could not remove a stored file (%s)", type(exc).__name__)
    return removed
