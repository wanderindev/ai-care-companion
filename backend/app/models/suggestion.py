"""Suggestions (DESIGN §5.6, §6.7 step 5): proposed changes waiting for a human.

The daily WhatsApp analysis (#39) and the chat (#41) never write records
directly: they add a ``suggestions`` row with ``app.services.suggestions.
create_suggestion``, and a family member approves (as proposed, or edited) or
rejects it in «Pendientes». Approval applies the change through the same
service code the normal API uses (``app.services.suggestions.approve``).

- ``kind``: ``create_entry``, ``update_entry``, ``create_appointment``,
  ``med_change``, ``lab_report`` (reserved: labs are extracted automatically,
  so it has no producer and no apply path in v1), ``doctor``.
- ``payload``: the proposed change, validated with the same Pydantic schemas
  as the target API (shapes in ``app.schemas.suggestions``).
- ``patient_id``: whose record it touches (null for a ``doctor``).
- ``source``: ``whatsapp``, ``chat`` or ``ai`` (a code-run AI producer with
  no message behind it — today only the doctor inference, #125, migration
  0028: an ``update_entry`` whose ``changes`` are only ``doctor_id``);
  ``import_id`` the WhatsApp import whose
  analysis produced it (``analysis_run_id`` the backfill run, #40);
  ``source_message_ids`` the ``whatsapp_messages`` it
  is based on (their files are attached to the created record). A chat
  suggestion (#41, migration 0021) has ``chat_session_id`` (the
  conversation) and ``created_by`` (the family member whose message led to
  it) and no source messages.
- ``status``: ``pending`` → ``approved`` | ``edited`` (approved with changes)
  | ``rejected``; decided once (``decided_by``, ``decided_at``,
  ``decision_note`` for a rejection reason). ``result_ref`` is
  ``{"entity": …, "id": …}`` of the record the approval wrote or linked.

PHI: ``payload`` and ``rationale`` hold medical content. Never log them.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

KINDS = (
    "create_entry",
    "update_entry",
    "create_appointment",
    "med_change",
    "lab_report",
    "doctor",
)
SOURCES = ("whatsapp", "chat", "ai")
SOURCE_AI = "ai"
STATUSES = ("pending", "approved", "edited", "rejected")
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_EDITED = "edited"
STATUS_REJECTED = "rejected"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class Suggestion(TimestampMixin, Base):
    __tablename__ = "suggestions"
    __table_args__ = (
        CheckConstraint(_in("kind", KINDS), name="ck_suggestions_kind"),
        CheckConstraint(_in("source", SOURCES), name="ck_suggestions_source"),
        CheckConstraint(_in("status", STATUSES), name="ck_suggestions_status"),
        Index("ix_suggestions_status", "status"),
        Index("ix_suggestions_patient_id", "patient_id"),
        Index("ix_suggestions_analysis_run_id", "analysis_run_id"),
        Index("ix_suggestions_chat_session_id", "chat_session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="whatsapp")
    patient_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="RESTRICT")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    source_message_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, server_default=text("'{}'::bigint[]")
    )
    import_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("whatsapp_imports.id", ondelete="SET NULL")
    )
    # The backfill analysis run that produced it (#40; null for daily/chat).
    analysis_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    # A chat suggestion (#41): its conversation and the proposing family member.
    chat_session_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=STATUS_PENDING)
    decided_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
