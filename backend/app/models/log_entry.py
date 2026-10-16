"""Bitácora entries (DESIGN §5.2): one row per visit, call, therapy, observation…

Enumerations are ``varchar`` + CHECK constraints (like ``attachments.owner_type``)
so adding a value is a one-line migration, not an ``ALTER TYPE``.

``search_vector`` is a stored generated column over title, source_label,
indications and family_notes, built with the ``spanish`` config on
*unaccented* text. ``unaccent()`` is only STABLE, so the expression goes
through ``public.immutable_unaccent(text)`` (migration 0004), an IMMUTABLE
wrapper that names the dictionary explicitly. The query side must apply the
same normalization: see ``app.services.log_entries.search_condition``.

Attachments point here with ``owner_type = 'log_entry'``; deleting an entry
unlinks them (``app.services.log_entries.delete_entry``).
"""

from datetime import date, time
from typing import Any

from sqlalchemy import (
    DDL,
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    Time,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.doctor import Doctor
from app.models.mixins import AuthorMixin, TimestampMixin

ENTRY_TYPES = (
    "consulta_control",
    "consulta_enfermedad",
    "visita_domiciliaria",
    "teleconsulta",
    "terapia",
    "vacuna",
    "procedimiento",
    "hospitalizacion",
    "examen",
    "observacion_familiar",
    "otro",
)
CHANNELS = ("presencial", "domicilio", "whatsapp", "telefono", "correo")
REVIEW_STATUSES = ("ok", "pending_review")
ORIGINS = ("import_docx", "whatsapp", "manual", "chat")

REVIEW_OK = "ok"
REVIEW_PENDING = "pending_review"
ORIGIN_MANUAL = "manual"

# Text search configuration used on both sides (vector and query).
TS_CONFIG = "spanish"

SEARCH_VECTOR_SQL = (
    "to_tsvector('spanish'::regconfig, public.immutable_unaccent("
    "coalesce(title, '') || ' ' || coalesce(source_label, '') || ' ' || "
    "coalesce(indications, '') || ' ' || coalesce(family_notes, '')))"
)

# Same function migration 0004 creates; here so ``Base.metadata.create_all``
# (the test schema) works on an empty database too.
IMMUTABLE_UNACCENT_DDL = """
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE OR REPLACE FUNCTION public.immutable_unaccent(text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$;
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class LogEntry(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "log_entries"
    __table_args__ = (
        CheckConstraint(_in("entry_type", ENTRY_TYPES), name="ck_log_entries_entry_type"),
        CheckConstraint(
            "channel IS NULL OR " + _in("channel", CHANNELS), name="ck_log_entries_channel"
        ),
        CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_log_entries_review_status"),
        CheckConstraint(_in("origin", ORIGINS), name="ck_log_entries_origin"),
        Index("ix_log_entries_patient_date", "patient_id", "entry_date"),
        Index("ix_log_entries_doctor_id", "doctor_id"),
        Index("ix_log_entries_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_time: Mapped[time | None] = mapped_column(Time)
    doctor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("doctors.id", ondelete="SET NULL")
    )
    source_label: Mapped[str | None] = mapped_column(String(200))
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(String(300))
    indications: Mapped[str | None] = mapped_column(Text)
    family_notes: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=REVIEW_OK, server_default=REVIEW_OK
    )
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ORIGIN_MANUAL, server_default=ORIGIN_MANUAL
    )
    origin_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True)
    )

    doctor: Mapped[Doctor | None] = relationship(lazy="selectin")


event.listen(LogEntry.__table__, "before_create", DDL(IMMUTABLE_UNACCENT_DDL))
