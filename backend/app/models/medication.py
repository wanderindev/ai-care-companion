"""Medications (DESIGN §5.4, §6.4): each patient's list, organized by time slot
like the family's printed sheet, with a version per save.

- ``med_items`` is the current state. A removed item is not deleted: its
  ``end_date`` is set (to *yesterday*, Panamá, so it is gone from today's
  list) and it stays in the table. Items whose ``end_date`` is before today
  are hidden from the list; items with a future ``start_date`` are listed.
  There is deliberately **no** ``end_date >= start_date`` check in the
  database: removing a not-yet-started item ends it yesterday, before its
  start (it was never taken). The API still rejects ``end_date <
  start_date`` in user input.
- ``days`` is an ``smallint[]`` of ISO weekdays (1 = lunes … 7 = domingo),
  sorted and unique; ``NULL`` means every day.
- ``med_avoid`` is the EVITAR list; a removed row is deleted (the version
  snapshots and the audit log keep it).
- ``med_list_versions`` holds one row per save with the full ``snapshot``
  (``app.services.meds_diff`` documents its shape) and the Spanish
  ``change_summary``. ``list_note`` is list-level text that is not one
  medication (e.g. a regimen table, markdown; later saves carry it forward)
  and ``effective_date`` the sheet's "Desde <fecha>" (null: the date of
  ``changed_at``); both are also snapshot keys (migration 0013).
- ``med_events`` is the history reconstructed from the bitácora (ticket #28),
  with a ``review_status`` like ``log_entries``. A derived event keeps its
  position in the entry's ``origin_ref.medication_events`` in
  ``source_index``; ``(source_entry_id, source_index)`` is unique (migration
  0014), which makes the backfill idempotent. Discarding an event is a hard
  delete (the audit row keeps it, and tells the backfill not to recreate it).

``prescriber_id`` is ``ON DELETE SET NULL`` (doctor merge repoints it, see
``app.services.doctors.doctor_references``); every ``source_entry_id`` is
``ON DELETE SET NULL`` because a bitácora delete is a hard delete.
"""

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.doctor import Doctor
from app.models.mixins import AuthorMixin, TimestampMixin

# Printed-sheet order (DESIGN §5.4).
SLOTS = (
    "al_despertar",
    "desayuno",
    "manana",
    "almuerzo",
    "tarde",
    "cena",
    "noche",
    "madrugada",
    "segun_necesidad",
    "otros",
)
EVENT_KINDS = ("inicio", "suspension", "cambio_dosis", "sustitucion")
REVIEW_STATUSES = ("ok", "pending_review")
REVIEW_OK = "ok"
REVIEW_PENDING = "pending_review"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _source_entry_fk() -> Any:
    return mapped_column(BigInteger, ForeignKey("log_entries.id", ondelete="SET NULL"))


def _patient_fk() -> Any:
    return mapped_column(BigInteger, ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False)


class MedItem(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "med_items"
    __table_args__ = (
        CheckConstraint(_in("slot", SLOTS), name="ck_med_items_slot"),
        CheckConstraint(
            "days IS NULL OR (cardinality(days) > 0 AND days <@ ARRAY[1,2,3,4,5,6,7]::smallint[])",
            name="ck_med_items_days",
        ),
        Index("ix_med_items_patient_slot", "patient_id", "slot", "sort_order"),
        Index("ix_med_items_prescriber_id", "prescriber_id"),
        Index("ix_med_items_source_entry_id", "source_entry_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int] = _patient_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    strength: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[str | None] = mapped_column(String(50))
    dose_note: Mapped[str | None] = mapped_column(String(200))
    slot: Mapped[str] = mapped_column(String(20), nullable=False)
    time: Mapped[dt.time | None] = mapped_column(Time)
    days: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger))
    schedule_note: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)
    prescriber_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("doctors.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(String(300))
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    source_entry_id: Mapped[int | None] = _source_entry_fk()

    prescriber: Mapped[Doctor | None] = relationship(lazy="selectin")


class MedAvoid(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "med_avoid"
    __table_args__ = (
        Index("ix_med_avoid_patient_id", "patient_id"),
        Index("ix_med_avoid_source_entry_id", "source_entry_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int] = _patient_fk()
    substance: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source_entry_id: Mapped[int | None] = _source_entry_fk()


class MedListVersion(TimestampMixin, Base):
    __tablename__ = "med_list_versions"
    __table_args__ = (
        UniqueConstraint("patient_id", "version_no", name="uq_med_list_versions_patient_no"),
        CheckConstraint("version_no >= 1", name="ck_med_list_versions_version_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int] = _patient_fk()
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    list_note: Mapped[str | None] = mapped_column(Text)
    effective_date: Mapped[dt.date | None] = mapped_column(Date)


class MedEvent(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "med_events"
    __table_args__ = (
        CheckConstraint(_in("kind", EVENT_KINDS), name="ck_med_events_kind"),
        CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_med_events_review_status"),
        Index("ix_med_events_patient_date", "patient_id", "event_date"),
        Index("ix_med_events_source_entry_id", "source_entry_id"),
        UniqueConstraint("source_entry_id", "source_index", name="uq_med_events_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int] = _patient_fk()
    event_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    medication: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    source_entry_id: Mapped[int | None] = _source_entry_fk()
    source_index: Mapped[int | None] = mapped_column(SmallInteger)
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=REVIEW_OK, server_default=REVIEW_OK
    )


class MedCheckDismissal(TimestampMixin, AuthorMixin, Base):
    """«Ya está reflejado» on the Medicamentos out-of-date banner (ticket #29,
    migration 0015): the family says a bitácora entry's medication changes are
    already in the list, so the check stops reporting it. Shared by the whole
    family (the list is), one row per entry; ``created_by`` / ``created_at``
    say who and when. Deleting the entry deletes the row (``CASCADE``: a
    dismissal means nothing without its entry)."""

    __tablename__ = "med_check_dismissals"
    __table_args__ = (
        UniqueConstraint("entry_id", name="uq_med_check_dismissals_entry"),
        Index("ix_med_check_dismissals_patient_id", "patient_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int] = _patient_fk()
    entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("log_entries.id", ondelete="CASCADE"), nullable=False
    )
