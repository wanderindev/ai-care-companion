"""Chat (DESIGN §5.7, §6.6): shared sessions and their messages.

- ``chat_sessions`` are **shared** by every family member (no per-user
  privacy). ``generating_since`` is the one-generation-per-session lock: set
  atomically when a turn starts, cleared when it ends; a value older than
  ``app.services.chat.engine.LOCK_STALE`` counts as free (a crashed worker
  never blocks a session for good). ``archived_at`` (#99, migration 0026)
  hides a session from the default list until it is restored; deleting one
  (``app.services.chat.sessions.delete_session``) cascades to its messages
  and leaves its suggestions with ``chat_session_id`` NULL.
- ``chat_messages`` hold one row per Messages API message, in order:
  ``content`` is the exact list of Anthropic content blocks that was (or will
  be) sent to the API — ``text``, ``thinking``, ``tool_use``, ``tool_result``
  — so the history replays byte-identically (prompt cache, thinking-block
  binding). A family member's turn is a ``user`` row with
  ``author_user_id`` set; tool results are ``user`` rows without an author.
  ``status`` is ``complete`` or ``interrupted`` (error, refusal, deadline:
  only its text blocks are replayed). ``meta`` holds bookkeeping that is
  never sent to the model: model id, stop reason, token usage, an error code,
  and later (#33) the ids of generated files.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import AuthorMixin, TimestampMixin

ROLES = ("user", "assistant")
STATUSES = ("complete", "interrupted")
STATUS_COMPLETE = "complete"
STATUS_INTERRUPTED = "interrupted"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class ChatSession(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generating_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessage(TimestampMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(_in("role", ROLES), name="ck_chat_messages_role"),
        CheckConstraint(_in("status", STATUSES), name="ck_chat_messages_status"),
        Index("ix_chat_messages_session_id_id", "session_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'complete'")
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
