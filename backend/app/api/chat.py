"""Chat API (DESIGN §6.6): shared sessions, and one streamed turn per message.

``POST /chat/sessions/{id}/messages`` answers with Server-Sent Events
(``text/event-stream``)::

    event: start     data: {"user_message_id": 12}
    event: delta     data: {"text": "Según la consulta…"}      (many)
    event: tool      data: {"name": "search_log_entries", "status": "Buscando en la bitácora…"}
    event: file      data: {"id": 7, "filename": "resumen-2026-10-09.pdf", "mime": "…", "size": 1234}
    event: suggestion data: {"id": 3, "kind": "create_entry", "patient_id": 1, "summary": "…"}
    event: error     data: {"code": "api_error", "detail": "<Spanish message>"}   (if any)
    event: message   data: {"id": 15, "status": "complete" | "interrupted"}
    event: title     data: {"title": "…"}                      (first exchange only)
    event: done      data: {}

plus ``: ping`` comment lines every ``HEARTBEAT_SECONDS`` while Claude or a
tool is busy, so no proxy closes an idle stream. Errors before the stream
starts are plain JSON responses: 404 (no session), 409 (another answer is
being generated in this session), 503 (no ``ANTHROPIC_API_KEY``), 422.

The turn runs in a background thread (``app.services.chat.engine``) that
does not depend on the connection: if the phone locks mid-answer, the answer
is still stored. The request's own database session is closed before the
stream starts; the turn opens short sessions per step.
"""

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.config import Settings, get_settings
from app.core.database import SessionLocal, get_db
from app.models.attachment import Attachment
from app.models.chat import ChatMessage, ChatSession
from app.models.suggestion import Suggestion
from app.models.user import User
from app.schemas.chat import (
    ChatAttachmentOut,
    ChatMessageCreate,
    ChatMessageOut,
    ChatSessionCreate,
    ChatSessionDetail,
    ChatSessionOut,
    ChatSessionPage,
    ChatSessionUpdate,
    ChatSuggestionOut,
    ChatToolCall,
)
from app.services import audit
from app.services import suggestions as suggestions_svc
from app.services.chat import engine, tools
from app.services.chat import sessions as sessions_svc
from app.services.storage import Storage, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

NOT_FOUND = "Conversación no encontrada."
BUSY = "Ya se está generando una respuesta en esta conversación. Espera a que termine."
FORBIDDEN = "Solo quien inició la conversación o un administrador puede eliminarla."
UNAVAILABLE = "El chat no está disponible: falta configurar la clave del servicio de IA."
HEARTBEAT_SECONDS = 15.0


# ── Dependencies (tests override them) ──────────────────────────────────────


def session_factory_dep() -> Callable[[], DbSession]:
    """Opens the short database sessions of a streamed turn."""
    return SessionLocal


@lru_cache(maxsize=2)
def _client_for(api_key: str):
    return engine.make_client(api_key)


def chat_client_dep(settings: Settings = Depends(get_settings)) -> Any:
    """The Anthropic client, or None without ``ANTHROPIC_API_KEY``."""
    if not settings.anthropic_api_key:
        return None
    return _client_for(settings.anthropic_api_key)


def storage_factory_dep() -> Callable[[], Storage]:
    """Gives the file tools the attachments' storage, called only when a file
    is stored (without Spaces keys the tool answers with an error; the chat
    itself keeps working)."""
    return get_storage


# ── Serialization ────────────────────────────────────────────────────────────


def _session_out(session: ChatSession, creator: str | None, user: User) -> ChatSessionOut:
    return ChatSessionOut(
        id=session.id,
        title=session.title,
        created_by=session.created_by,
        created_by_name=creator,
        created_at=session.created_at,
        last_message_at=session.last_message_at,
        generating=engine.is_generating(session),
        archived_at=session.archived_at,
        can_delete=sessions_svc.can_delete(session, user),
    )


def _message_out(
    row: ChatMessage,
    names: dict[int, str],
    files: dict[int, list[ChatAttachmentOut]] | None = None,
    proposals: dict[int, ChatSuggestionOut] | None = None,
) -> ChatMessageOut:
    if row.role == "user":
        return ChatMessageOut(
            id=row.id,
            kind="prompt",
            author_user_id=row.author_user_id,
            author_name=names.get(row.author_user_id) if row.author_user_id else None,
            text=engine.prompt_text(row.content),
            tools=[],
            status=row.status,
            error=None,
            created_at=row.created_at,
        )
    calls = [
        ChatToolCall(name=b.get("name", ""), status=tools.status_for(b.get("name", "")))
        for b in row.content
        if b.get("type") == "tool_use"
    ]
    return ChatMessageOut(
        id=row.id,
        kind="assistant",
        author_user_id=None,
        author_name=None,
        text=engine.answer_text(row.content),
        tools=calls,
        status=row.status,
        error=(row.meta or {}).get("error"),
        created_at=row.created_at,
        attachments=(files or {}).get(row.id, []),
        suggestions=[
            (proposals or {})[i]
            for i in (row.meta or {}).get("suggestion_ids", [])
            if i in (proposals or {})
        ],
    )


def _files(db: DbSession, message_ids: list[int]) -> dict[int, list[ChatAttachmentOut]]:
    """Files owned by these messages (generated PDF/XLSX), by message id."""
    if not message_ids:
        return {}
    out: dict[int, list[ChatAttachmentOut]] = {}
    for att in db.scalars(
        select(Attachment)
        .where(Attachment.owner_type == "chat_message", Attachment.owner_id.in_(message_ids))
        .order_by(Attachment.id)
    ):
        out.setdefault(att.owner_id, []).append(ChatAttachmentOut.model_validate(att))
    return out


def _proposals(db: DbSession, rows: list[ChatMessage]) -> dict[int, ChatSuggestionOut]:
    """The suggestions the answers proposed (``meta.suggestion_ids``), by id,
    with their current status."""
    ids = {i for r in rows for i in (r.meta or {}).get("suggestion_ids", [])}
    if not ids:
        return {}
    return {
        s.id: ChatSuggestionOut(
            id=s.id,
            kind=s.kind,
            patient_id=s.patient_id,
            summary=suggestions_svc.short_summary(db, s.kind, s.payload or {}),
            status=s.status,
        )
        for s in db.scalars(select(Suggestion).where(Suggestion.id.in_(ids)))
    }


def _names(db: DbSession, ids: set[int | None]) -> dict[int, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.display_name).where(User.id.in_(wanted))).all())


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ── Sessions ─────────────────────────────────────────────────────────────────


@router.get("/sessions", response_model=ChatSessionPage)
def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    archived: bool = Query(False),
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> ChatSessionPage:
    """Active sessions by default; ``archived=true``: the archived ones only."""
    where = ChatSession.archived_at.is_not(None) if archived else ChatSession.archived_at.is_(None)
    total = db.scalar(select(func.count()).select_from(ChatSession).where(where)) or 0
    rows = list(
        db.scalars(
            select(ChatSession)
            .where(where)
            .order_by(
                func.coalesce(ChatSession.last_message_at, ChatSession.created_at).desc(),
                ChatSession.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    )
    names = _names(db, {r.created_by for r in rows})
    return ChatSessionPage(
        items=[
            _session_out(r, names.get(r.created_by) if r.created_by else None, user) for r in rows
        ],
        total=total,
    )


@router.post("/sessions", response_model=ChatSessionOut, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: ChatSessionCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> ChatSessionOut:
    session = ChatSession(title=payload.title, created_by=user.id, updated_by=user.id)
    db.add(session)
    db.flush()
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_CREATE,
        entity="chat_session",
        entity_id=session.id,
        after=audit.snapshot(session),
    )
    db.commit()
    db.refresh(session)
    return _session_out(session, user.display_name, user)


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail)
def get_session(
    session_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> ChatSessionDetail:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    rows = list(
        db.scalars(
            select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
        )
    )
    # Tool results are internal to a turn: the UI shows the calls (assistant
    # ``tools``), not the data returned.
    visible = [r for r in rows if r.role == "assistant" or r.author_user_id is not None]
    names = _names(db, {r.author_user_id for r in visible} | {session.created_by})
    files = _files(db, [r.id for r in visible if r.role == "assistant"])
    proposals = _proposals(db, [r for r in visible if r.role == "assistant"])
    out = _session_out(session, names.get(session.created_by) if session.created_by else None, user)
    return ChatSessionDetail(
        **out.model_dump(),
        messages=[_message_out(r, names, files, proposals) for r in visible],
    )


@router.patch("/sessions/{session_id}", response_model=ChatSessionOut)
def update_session(
    session_id: int,
    payload: ChatSessionUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> ChatSessionOut:
    """Archive or restore (any member; sessions are shared). 409 while a turn
    is generating."""
    try:
        session = sessions_svc.set_archived(db, session_id, payload.archived, user=user)
    except sessions_svc.SessionNotFound:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND) from None
    except sessions_svc.SessionBusy:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=BUSY) from None
    db.commit()
    db.refresh(session)
    names = _names(db, {session.created_by})
    return _session_out(
        session, names.get(session.created_by) if session.created_by else None, user
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
    storage_factory: Callable[[], Storage] = Depends(storage_factory_dep),
) -> None:
    """Hard delete (admins and the session's creator; 403 otherwise, 409 while
    a turn is generating). See ``app.services.chat.sessions``."""
    try:
        result = sessions_svc.delete_session(db, session_id, user=user)
    except sessions_svc.SessionNotFound:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND) from None
    except sessions_svc.SessionBusy:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=BUSY) from None
    except sessions_svc.DeleteForbidden:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN) from None
    db.commit()
    sessions_svc.purge_objects(db, result.orphan_keys, storage_factory)


# ── A turn ───────────────────────────────────────────────────────────────────


def _produce(runner: engine.TurnRunner, events: queue.Queue) -> None:
    try:
        for event in runner.run():
            events.put(event)
    except Exception as exc:  # never let the thread die silently
        logger.error("chat: turn crashed session=%s (%s)", runner.session_id, type(exc).__name__)
        events.put(
            engine.Event("error", {"code": "internal", "detail": engine.ERROR_MESSAGES["internal"]})
        )
    finally:
        events.put(None)


def _stream(events: queue.Queue, user_message_id: int) -> Iterator[str]:
    yield _sse("start", {"user_message_id": user_message_id})
    while True:
        try:
            event = events.get(timeout=HEARTBEAT_SECONDS)
        except queue.Empty:
            yield ": ping\n\n"
            continue
        if event is None:
            return
        yield _sse(event.type, event.data)


@router.post("/sessions/{session_id}/messages")
def post_message(
    session_id: int,
    payload: ChatMessageCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
    settings: Settings = Depends(get_settings),
    client: Any = Depends(chat_client_dep),
    session_factory: Callable[[], DbSession] = Depends(session_factory_dep),
    storage_factory: Callable[[], Storage] = Depends(storage_factory_dep),
) -> StreamingResponse:
    if client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE)
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    lock_token = engine.acquire_lock(db, session_id)
    if lock_token is None:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=BUSY)
    try:
        message = engine.begin_turn(db, session, user, payload.text)
        db.commit()  # the lock and the message, together
    except Exception:
        db.rollback()  # also undoes the lock
        raise
    user_message_id = message.id
    user_id = user.id
    # Give the connection back before streaming: the turn may take minutes.
    db.close()

    runner = engine.TurnRunner(
        session_factory=session_factory,
        client=client,
        session_id=session_id,
        user_id=user_id,
        lock_token=lock_token,
        model=settings.chat_model,
        title_model=settings.chat_title_model,
        max_tool_iterations=max(1, min(20, settings.chat_max_tool_iterations)),
        storage_factory=storage_factory,
    )
    events: queue.Queue = queue.Queue()
    threading.Thread(
        target=_produce, args=(runner, events), name=f"chat-turn-{session_id}", daemon=True
    ).start()
    return StreamingResponse(
        _stream(events, user_message_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
