"""Appointments (DESIGN §5.3): the family calendar.

``starts_at`` is a ``timestamptz``; the API reads and writes it as ISO 8601
with an offset and returns it on the America/Panama clock (UTC-5, no DST). An
**all-day** appointment (a date without a time, e.g. "Próxima cita 13 ABR")
has ``all_day = true`` and ``starts_at`` at 00:00 America/Panama of that day;
``duration_min`` is then ignored.

``created_from_entry_id`` is the bitácora entry whose "Próxima cita" created
the appointment (unique: one appointment per entry, which keeps the bitácora
form and the backfill idempotent); ``result_entry_id`` is the entry written
when it happened. Both are ``ON DELETE SET NULL``: deleting an entry (a hard
delete) never deletes an appointment.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.doctor import Doctor
from app.models.mixins import AuthorMixin, TimestampMixin

STATUSES = ("programada", "realizada", "cancelada")
STATUS_SCHEDULED = "programada"
STATUS_DONE = "realizada"
STATUS_CANCELLED = "cancelada"

DEFAULT_DURATION_MIN = 30
MAX_DURATION_MIN = 1440


class Appointment(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")",
            name="ck_appointments_status",
        ),
        CheckConstraint(
            f"duration_min BETWEEN 1 AND {MAX_DURATION_MIN}", name="ck_appointments_duration"
        ),
        Index("ix_appointments_patient_starts_at", "patient_id", "starts_at"),
        Index("ix_appointments_status", "status"),
        Index("ix_appointments_doctor_id", "doctor_id"),
        Index("ix_appointments_result_entry_id", "result_entry_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    doctor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("doctors.id", ondelete="SET NULL")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_min: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_DURATION_MIN,
        server_default=text(str(DEFAULT_DURATION_MIN)),
    )
    all_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    location: Mapped[str | None] = mapped_column(String(300))
    reason: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_SCHEDULED, server_default=STATUS_SCHEDULED
    )
    created_from_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("log_entries.id", ondelete="SET NULL"), unique=True
    )
    result_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("log_entries.id", ondelete="SET NULL")
    )

    doctor: Mapped[Doctor | None] = relationship(lazy="selectin")
