"""One chat turn: the streaming tool-use loop, its events and its persistence.

Flow (``POST /chat/sessions/{id}/messages``):

1. In the request (``begin_turn``): take the session's generation lock
   (``acquire_lock``: one generation per session across workers; 409
   otherwise), store the family member's message (a context line + their
   text), commit, and **close** the request's database session.
2. In a background thread (``TurnRunner.run``, see ``app.api.chat``): load
   the history in a short session, then loop — stream a Messages API request
   (text deltas become ``delta`` events), run the requested tools (a ``tool``
   event each, every call in its own short session), append the
   ``tool_use`` / ``tool_result`` pair, and ask again — until Claude answers
   without tools. At most ``CHAT_MAX_TOOL_ITERATIONS`` tool rounds: the next
   request sets ``tool_choice: none`` so Claude must answer with what it has.
3. Persist every new message in one short session and release the lock
   (``message`` event with the final assistant message id), then title a new
   session (``title`` event) and end (``done``).

**No database connection is held while Claude is generating**: sessions are
opened only around the history load, each tool call and the final write
(``session_factory()`` per step). The API's pool is 2 + 2 per process.

Files (``generate_pdf`` / ``generate_xlsx``, #33; ``previsit_summary`` /
``emergency_card``, #34): the tool stores the file
as an unlinked attachment while the turn runs and the engine sends a
``file`` event ``{id, filename, mime, size}``; ``_persist`` links every file
of the turn to the final assistant message (``owner_type='chat_message'``,
also when it is ``interrupted``) in the same transaction as the insert, and
their ids go in its ``meta.attachment_ids``. A turn that stores nothing
(lost lock, storage error) leaves its files unlinked, like an abandoned
upload.

Proposals (``propose_change``, #41): the tool commits a ``pending``
suggestion at once (it is in «Pendientes» whatever happens to the turn) and
the engine sends a ``suggestion`` event ``{id, kind, patient_id, summary}``;
the ids go in the final assistant message's ``meta.suggestion_ids`` so the
stored session shows the card again. A turn that stores nothing leaves the
suggestion in «Pendientes» without a card in the conversation.

An API error, a refusal, a cut at ``max_tokens`` or the turn deadline still
stores the completed tool rounds and the partial text, as an ``interrupted``
assistant message, and sends an ``error`` event. The turn keeps running if
the browser goes away (the thread does not depend on the connection), so the
answer appears in the session when the family member comes back.

Requests: ``CHAT_MODEL`` with adaptive thinking at medium effort; the static
system prompt and the tool definitions are cached (a breakpoint on the
system block, which follows the tools), and the conversation through
top-level automatic caching. Thinking blocks are replayed as stored;
``block_binding.prefix_mismatch_behavior = "drop_block"`` makes the API drop
a block that no longer matches (after a deploy changes the system prompt or
the tools, or after the history cap) instead of failing the turn. Models in
``FALLBACK_MODELS`` also get the server-side refusal fallback.

Logs carry ids, counts, stop reasons and token usage only — never message
text, tool inputs or tool results.
"""

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session as DbSession

from app.models.chat import STATUS_COMPLETE, STATUS_INTERRUPTED, ChatMessage, ChatSession
from app.models.user import User
from app.services import attachments, audit
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, Usage
from app.services.chat import history, prompt, title, tools

logger = logging.getLogger(__name__)

MAX_TOKENS = 16_000
EFFORT = "medium"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
# Per request: the timeout is per read (a streamed answer keeps it alive),
# one retry on 408/409/429/5xx and connection errors.
REQUEST_TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 1
# A turn starts no new request and runs no more tools after this long (the
# answer so far is stored as interrupted).
TURN_DEADLINE_SECONDS = 480
# The generation lock counts as stale (free) after LOCK_STALE. It must exceed
# the worst-case turn: the deadline (8 min) + the request already running
# then (at most MAX_TOKENS of output, ~5 min, twice with the retry) + its
# tools (seconds) + the title (title.TITLE_TIMEOUT_SECONDS, one retry: 1 min)
# = about 20 min. Even past it nothing is corrupted: the lock is cleared only
# by the turn that owns it (compare-and-clear on its timestamp), and a turn
# that lost its lock stores nothing (``LockLost``). A session left locked by
# a worker restart frees itself after LOCK_STALE.
LOCK_STALE = timedelta(minutes=30)
MAX_TEXT_CHARS = 8_000
REPLAY_TYPES = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})

ERROR_MESSAGES = {
    "api_error": "No pude completar la respuesta por un error del servicio de IA. "
    "Intenta de nuevo en un momento.",
    "rate_limited": "El servicio de IA está saturado. Intenta de nuevo en un minuto.",
    "connection": "No pude conectar con el servicio de IA. Intenta de nuevo.",
    "refusal": "No puedo responder a esa pregunta. Prueba a formularla de otra manera.",
    "truncated": "La respuesta era demasiado larga y quedó cortada.",
    "deadline": "La respuesta tardó demasiado y quedó incompleta.",
    "internal": "Ocurrió un error inesperado al responder.",
    "storage": "No pude guardar la respuesta.",
    "lock_lost": "La respuesta tardó demasiado y no se guardó. Vuelve a preguntar.",
}

_clock = time.monotonic  # tests replace it to reach the deadline


class LockLost(RuntimeError):
    """The turn's lock went stale and another turn took the session."""


def make_client(api_key: str):
    """The real Anthropic client (tests pass a fake with the same surface)."""
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
    )


# ── Lock and the family member's message (in the request) ───────────────────


def acquire_lock(db: DbSession, session_id: int, now: datetime | None = None) -> datetime | None:
    """Atomically mark the session as generating.

    Returns the lock's timestamp (the turn's token for :func:`release_lock`)
    or None if another turn runs."""
    now = now or datetime.now(UTC)
    row = db.execute(
        update(ChatSession)
        .where(
            ChatSession.id == session_id,
            or_(
                ChatSession.generating_since.is_(None),
                ChatSession.generating_since < now - LOCK_STALE,
            ),
        )
        .values(generating_since=now)
        .returning(ChatSession.generating_since)
    ).first()
    return row[0] if row is not None else None


def release_lock(db: DbSession, session_id: int, token: datetime) -> None:
    """Clear the lock only if it is still this turn's (compare-and-clear)."""
    db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.generating_since == token)
        .values(generating_since=None)
    )


def is_generating(session: ChatSession, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return session.generating_since is not None and session.generating_since >= now - LOCK_STALE


def prompt_content(author_name: str, text: str, now: datetime) -> list[dict[str, Any]]:
    """A family member's message as stored and sent: context line, then the text."""
    return [
        {"type": "text", "text": prompt.context_line(author_name, now)},
        {"type": "text", "text": text},
    ]


def prompt_text(content: list[dict[str, Any]]) -> str:
    """The typed text of a stored family message (without the context line)."""
    texts = [b.get("text", "") for b in content if b.get("type") == "text"]
    return texts[-1] if texts else ""


def begin_turn(db: DbSession, session: ChatSession, user: User, text: str) -> ChatMessage:
    """Store the family member's message (lock already held); the caller commits."""
    now = datetime.now(UTC)
    message = ChatMessage(
        session_id=session.id,
        role="user",
        content=prompt_content(user.display_name, text, now),
        author_user_id=user.id,
        status=STATUS_COMPLETE,
    )
    db.add(message)
    session.last_message_at = now
    db.flush()
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_CREATE,
        entity="chat_message",
        entity_id=message.id,
        after={"session_id": session.id, "role": "user"},
    )
    return message


# ── Requests ─────────────────────────────────────────────────────────────────


def request_params(
    model: str, messages: list[dict[str, Any]], *, allow_tools: bool = True
) -> dict[str, Any]:
    betas = [BINDING_BETA]
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        # Tools render before the system prompt: this breakpoint caches both.
        "system": [
            {
                "type": "text",
                "text": prompt.SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": tools.definitions(),
        "messages": messages,
        "thinking": {
            "type": "adaptive",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        },
        "output_config": {"effort": EFFORT},
        # Automatic caching of the conversation so far (tool rounds re-send it).
        "cache_control": {"type": "ephemeral"},
    }
    if not allow_tools:
        # Same tools array (it is part of the cached, bound prefix), no calls.
        params["tool_choice"] = {"type": "none"}
    if model in FALLBACK_MODELS:
        betas.append(FALLBACK_BETA)
        params["fallbacks"] = "default"
    params["betas"] = betas
    return params


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    return block.to_dict(mode="json")


def replayable(content: list[Any]) -> list[dict[str, Any]]:
    """A response's content as blocks that can be stored and sent back.

    After a mid-output refusal fallback (a ``fallback`` block), the declined
    partial's non-text blocks are omitted (API rule for echoing fallback
    turns); the marker itself and any block type this code does not know are
    dropped.
    """
    blocks = [_block_dict(b) for b in content]
    cut = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if cut is not None:
        blocks = [b for b in blocks[:cut] if b.get("type") == "text"] + blocks[cut + 1 :]
    return [b for b in blocks if b.get("type") in REPLAY_TYPES]


def answer_text(content: list[dict[str, Any]]) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


# ── The turn ─────────────────────────────────────────────────────────────────


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _NewMessage:
    role: str
    content: list[dict[str, Any]]
    status: str = STATUS_COMPLETE
    meta: dict[str, Any] | None = None


def _error_code(exc: BaseException) -> str:
    import anthropic

    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited"
    if isinstance(exc, anthropic.APIStatusError):
        return "api_error"
    if isinstance(exc, anthropic.APIConnectionError):
        return "connection"
    return "internal"


@dataclass
class TurnRunner:
    session_factory: Callable[[], DbSession]
    client: Any
    session_id: int
    user_id: int | None
    lock_token: datetime
    model: str
    title_model: str
    max_tool_iterations: int = 8
    deadline_seconds: float = TURN_DEADLINE_SECONDS
    # Returns the attachments' Storage for the file tools (None: no file tools).
    storage_factory: Callable[[], Any] | None = None
    # Attachments the file tools of this turn stored (unlinked until _persist).
    attachment_ids: list[int] = field(default_factory=list)
    # Suggestions propose_change created in this turn (already committed).
    suggestion_ids: list[int] = field(default_factory=list)

    def run(self) -> Iterator[Event]:
        try:
            yield from self._run()
        finally:
            # Normally released with the final write; this covers a crash
            # before it. Compare-and-clear: never another turn's lock.
            self._release()

    def _release(self) -> None:
        try:
            with self.session_factory() as db:
                release_lock(db, self.session_id, self.lock_token)
                db.commit()
        except Exception:
            logger.error("chat: could not release lock session=%s", self.session_id)

    def _load(self) -> tuple[list[dict[str, Any]], int, bool, str | None, str]:
        with self.session_factory() as db:
            session = db.get(ChatSession, self.session_id)
            rows = list(
                db.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == self.session_id)
                    .order_by(ChatMessage.id)
                )
            )
            stored = [
                history.StoredMessage(
                    role=r.role,
                    content=r.content,
                    status=r.status,
                    is_prompt=r.role == "user" and r.author_user_id is not None,
                )
                for r in rows
            ]
            first_exchange = not any(r.role == "assistant" for r in rows)
            prompts = [r for r in rows if r.role == "user" and r.author_user_id is not None]
            last_prompt = prompt_text(prompts[-1].content) if prompts else ""
            current_title = session.title if session else None
        messages, omitted = history.build_messages(stored)
        return messages, omitted, first_exchange, current_title, last_prompt

    def _run_tools(self, uses: list[dict[str, Any]]) -> Iterator[Event | dict[str, Any]]:
        for use in uses:
            yield Event("tool", {"name": use.get("name"), "status": tools.status_for(use["name"])})
            with self.session_factory() as db:
                ctx = tools.ToolContext(
                    db=db,
                    user_id=self.user_id,
                    session_id=self.session_id,
                    storage=self.storage_factory,
                )
                try:
                    run = tools.run_tool(ctx, use["id"], use.get("name", ""), use.get("input"))
                except Exception as exc:
                    logger.error(
                        "chat: tool %s failed session=%s (%s)",
                        use.get("name"),
                        self.session_id,
                        type(exc).__name__,
                    )
                    run = tools.ToolRun(
                        block={
                            "type": "tool_result",
                            "tool_use_id": use["id"],
                            "content": "Error interno al consultar los datos.",
                            "is_error": True,
                        },
                        ok=False,
                    )
                finally:
                    # Read-only tools keep nothing; a file tool committed its file.
                    db.rollback()
                self.attachment_ids.extend(ctx.attachment_ids)
                self.suggestion_ids.extend(info["id"] for info in ctx.suggestions)
            for info in ctx.files:
                yield Event("file", info)
            for info in ctx.suggestions:
                yield Event("suggestion", info)
            yield run.block

    def _run(self) -> Iterator[Event]:
        started = _clock()
        messages, omitted, first_exchange, current_title, last_prompt = self._load()
        new: list[_NewMessage] = []
        usage = Usage()
        rounds = 0
        stop_reason: str | None = None
        error_code: str | None = None
        served_by: str | None = None
        partial: list[str] = []

        try:
            while True:
                if _clock() - started > self.deadline_seconds:
                    error_code = "deadline"
                    break
                allow_tools = rounds < self.max_tool_iterations
                partial = []
                params = request_params(self.model, messages, allow_tools=allow_tools)
                with self.client.beta.messages.stream(**params) as stream:
                    for event in stream:
                        if getattr(event, "type", None) == "text" and event.text:
                            partial.append(event.text)
                            yield Event("delta", {"text": event.text})
                    final = stream.get_final_message()
                usage.add(final.usage)
                served_by = getattr(final, "model", None) or served_by
                stop_reason = final.stop_reason
                content = replayable(final.content)
                # The streamed text is in ``content`` now: never store it twice.
                partial = []
                uses = [b for b in content if b.get("type") == "tool_use"]

                if stop_reason == "tool_use" and uses and allow_tools:
                    if _clock() - started > self.deadline_seconds:
                        # No time left to run the tools: keep the text only.
                        error_code = "deadline"
                        content = [b for b in content if b.get("type") != "tool_use"]
                        new.append(_NewMessage("assistant", content, status=STATUS_INTERRUPTED))
                        break
                    new.append(_NewMessage("assistant", content, meta={"stop_reason": "tool_use"}))
                    messages.append({"role": "assistant", "content": content})
                    results: list[dict[str, Any]] = []
                    for item in self._run_tools(uses):
                        if isinstance(item, Event):
                            yield item
                        else:
                            results.append(item)
                    new.append(_NewMessage("user", results))
                    messages.append({"role": "user", "content": results})
                    rounds += 1
                    continue

                # The answer (or the end of the road): no tool calls are run.
                content = [b for b in content if b.get("type") != "tool_use"]
                status = STATUS_COMPLETE
                if stop_reason == "refusal":
                    error_code, status = "refusal", STATUS_INTERRUPTED
                elif stop_reason == "max_tokens":
                    error_code, status = "truncated", STATUS_INTERRUPTED
                new.append(_NewMessage("assistant", content, status=status))
                break
        except Exception as exc:
            error_code = _error_code(exc)
            logger.error(
                "chat: request failed session=%s round=%s (%s)",
                self.session_id,
                rounds,
                type(exc).__name__,
            )
            if partial:
                text = "".join(partial)
                new.append(
                    _NewMessage(
                        "assistant", [{"type": "text", "text": text}], status=STATUS_INTERRUPTED
                    )
                )

        if not new or new[-1].role != "assistant":
            # Nothing to show for the last step: keep the history valid and
            # visible with an empty interrupted message (never replayed).
            new.append(_NewMessage("assistant", [], status=STATUS_INTERRUPTED))
        final_msg = new[-1]
        if error_code:
            final_msg.status = STATUS_INTERRUPTED
        final_msg.meta = {
            **(final_msg.meta or {}),
            "model": served_by or self.model,
            "stop_reason": stop_reason,
            "tool_rounds": rounds,
            "usage": usage.as_dict(),
            **({"error": error_code} if error_code else {}),
            **({"omitted_messages": omitted} if omitted else {}),
            **({"attachment_ids": self.attachment_ids} if self.attachment_ids else {}),
            **({"suggestion_ids": self.suggestion_ids} if self.suggestion_ids else {}),
        }
        logger.info(
            "chat turn session=%s rounds=%s stop=%s error=%s usage=%s",
            self.session_id,
            rounds,
            stop_reason,
            error_code,
            usage.as_dict(),
        )
        if error_code:
            yield Event("error", {"code": error_code, "detail": ERROR_MESSAGES[error_code]})

        try:
            final_id = self._persist(new)
        except LockLost:
            logger.error("chat: lock lost, turn not stored session=%s", self.session_id)
            yield Event("error", {"code": "lock_lost", "detail": ERROR_MESSAGES["lock_lost"]})
            return
        except Exception as exc:
            logger.error(
                "chat: could not store the turn session=%s (%s)",
                self.session_id,
                type(exc).__name__,
            )
            yield Event("error", {"code": "storage", "detail": ERROR_MESSAGES["storage"]})
            return
        yield Event("message", {"id": final_id, "status": final_msg.status})

        if first_exchange and current_title is None:
            answer = answer_text(final_msg.content) if final_msg.status == STATUS_COMPLETE else ""
            new_title = title.make_title(self.client, self.title_model, last_prompt, answer)
            if self._store_title(new_title):
                yield Event("title", {"title": new_title})
        yield Event("done", {})

    def _persist(self, new: list[_NewMessage]) -> int:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            owner = db.scalar(
                select(ChatSession.generating_since)
                .where(ChatSession.id == self.session_id)
                .with_for_update()
            )
            if owner != self.lock_token:
                # Another turn owns the session now: its prompt may already be
                # stored, so these rows would land after it.
                db.rollback()
                raise LockLost()
            rows = [
                ChatMessage(
                    session_id=self.session_id,
                    role=m.role,
                    content=m.content,
                    status=m.status,
                    meta=m.meta,
                )
                for m in new
            ]
            db.add_all(rows)
            if self.attachment_ids:
                # The turn's files belong to its final assistant message (also
                # an interrupted one), linked in the same transaction.
                db.flush()
                attachments.link_attachments(
                    db,
                    self.attachment_ids,
                    owner_type="chat_message",
                    owner_id=rows[-1].id,
                    user_id=self.user_id,
                )
            db.execute(
                update(ChatSession)
                .where(ChatSession.id == self.session_id)
                .values(last_message_at=now, generating_since=None)
            )
            db.commit()
            return rows[-1].id

    def _store_title(self, value: str) -> bool:
        if not value:
            return False
        with self.session_factory() as db:
            result = db.execute(
                update(ChatSession)
                .where(ChatSession.id == self.session_id, ChatSession.title.is_(None))
                .values(title=value)
            )
            db.commit()
            return bool(result.rowcount)
