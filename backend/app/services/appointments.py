"""Appointments: create/update/delete with audit, listing, and the bitácora's
"Próxima cita" (DESIGN §5.3, §6.1, §6.3).

Times: ``starts_at`` is stored as ``timestamptz`` and shown on the
America/Panama clock (UTC-5, no DST). A naive datetime from a client is read
as Panama time. An all-day appointment keeps ``starts_at`` at 00:00 Panama of
its day (``normalize_starts_at``).

Status: ``programada`` → ``realizada`` (with ``result_entry_id``, the entry
written when it happened) or ``cancelada``. Cancelling is a ``PATCH``; a
``DELETE`` is a hard delete for mistakes, and its audit row keeps the full
appointment.

"Próxima cita" (``sync_next_appointment``): an entry owns at most one
appointment, linked by ``created_from_entry_id`` (unique). Saving the entry
with a next appointment creates it, or updates it and sets it back to
``programada`` if it had been cancelled. Clearing it **cancels** it (never
deletes: someone may have added notes in the calendar, and a cancelled row
can come back if the field is filled again). A ``realizada`` appointment is
never changed from the entry form (``AppointmentError``).

Email invites: create / update / delete call the hooks of
``app.services.calendar_invites`` (which queue an ``appointment_invite``
job in the same transaction) unless ``notify=False``. Every write path goes
through these three functions: the API, ``/complete``, and the bitácora sync
above; only the backfill opts out.
"""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.orm import Session as DbSession

from app.models.appointment import (
    MAX_DURATION_MIN,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_SCHEDULED,
    STATUSES,
    Appointment,
)
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.models.patient import Patient
from app.services import audit
from app.services.whatsapp.chat import PANAMA_TZ

ENTITY = "appointment"
MIN_YEAR = 1900
MAX_YEAR = 2100

EDITABLE_FIELDS = frozenset(
    {
        "patient_id",
        "doctor_id",
        "starts_at",
        "duration_min",
        "all_day",
        "location",
        "reason",
        "notes",
        "status",
        "result_entry_id",
    }
)
CREATE_FIELDS = EDITABLE_FIELDS - {"status", "result_entry_id"}


class AppointmentError(ValueError):
    """Invalid appointment data; the message is Spanish and safe to show."""


# ── Time ─────────────────────────────────────────────────────────────────────


def to_local(value: datetime) -> datetime:
    """``value`` on the America/Panama clock (a naive value is taken as Panama)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=PANAMA_TZ)
    return value.astimezone(PANAMA_TZ)


def local_datetime(day: date, at: time | None = None) -> datetime:
    """Aware datetime for a Panama date (midnight when ``at`` is None)."""
    return datetime.combine(day, at or time(0, 0), tzinfo=PANAMA_TZ)


def normalize_starts_at(value: datetime, all_day: bool) -> datetime:
    local = to_local(value)
    if all_day:
        return local_datetime(local.date())
    return local


def today_local() -> date:
    return datetime.now(PANAMA_TZ).date()


# ── Writes ───────────────────────────────────────────────────────────────────


def validate(db: DbSession, appt: Appointment) -> None:
    """Rules for every write path. Raises ``AppointmentError``."""
    if appt.patient_id is None or db.get(Patient, appt.patient_id) is None:
        raise AppointmentError("El paciente no existe.")
    if appt.doctor_id is not None and db.get(Doctor, appt.doctor_id) is None:
        raise AppointmentError("El médico no existe.")
    if appt.status not in STATUSES:
        raise AppointmentError("Estado no válido.")
    if not 1 <= (appt.duration_min or 0) <= MAX_DURATION_MIN:
        raise AppointmentError("La duración debe estar entre 1 minuto y 24 horas.")
    if not MIN_YEAR <= to_local(appt.starts_at).year <= MAX_YEAR:
        raise AppointmentError("La fecha no es válida.")
    if appt.result_entry_id is not None:
        result = db.get(LogEntry, appt.result_entry_id)
        if result is None:
            raise AppointmentError("La entrada de la bitácora no existe.")
        if result.patient_id != appt.patient_id:
            raise AppointmentError("La entrada de la bitácora es de otro paciente.")


def _check_fields(fields: set[str], allowed: frozenset[str]) -> None:
    unknown = fields - allowed
    if unknown:
        raise AppointmentError(f"Campos no permitidos: {', '.join(sorted(unknown))}.")


def create_appointment(
    db: DbSession,
    data: dict[str, Any],
    *,
    user_id: int | None,
    status: str = STATUS_SCHEDULED,
    created_from_entry_id: int | None = None,
    result_entry_id: int | None = None,
    notify: bool = True,
) -> Appointment:
    """Validate, insert and audit. The caller commits (or rolls back on
    ``AppointmentError``). ``notify=False`` queues no email invite (the
    backfill); see ``app.services.calendar_invites``."""
    _check_fields(set(data), CREATE_FIELDS)
    values = dict(data)
    all_day = bool(values.get("all_day", False))
    values["starts_at"] = normalize_starts_at(values["starts_at"], all_day)
    appt = Appointment(
        **values,
        status=status,
        created_from_entry_id=created_from_entry_id,
        result_entry_id=result_entry_id,
        created_by=user_id,
        updated_by=user_id,
    )
    if appt.duration_min is None:
        appt.duration_min = Appointment.duration_min.default.arg
    if appt.all_day is None:
        appt.all_day = False
    validate(db, appt)
    db.add(appt)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity=ENTITY,
        entity_id=appt.id,
        after=audit.snapshot(appt),
    )
    if notify:
        from app.services import calendar_invites

        calendar_invites.on_create(db, appt)
    return appt


def update_appointment(
    db: DbSession,
    appt: Appointment,
    changes: dict[str, Any],
    *,
    user_id: int | None,
    notify: bool = True,
) -> Appointment:
    """Apply ``changes`` (audited only when something changed). The caller
    commits. A meaningful change queues an email invite unless ``notify`` is
    false."""
    _check_fields(set(changes), EDITABLE_FIELDS)
    before = audit.snapshot(appt)
    for field, value in changes.items():
        if getattr(appt, field) != value:
            setattr(appt, field, value)
    starts_at = normalize_starts_at(appt.starts_at, appt.all_day)
    # Same instant on another clock is no change (and no audit noise).
    if starts_at != appt.starts_at:
        appt.starts_at = starts_at
    validate(db, appt)
    db.flush()
    after = audit.snapshot(appt)
    if after != before:
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_UPDATE,
            entity=ENTITY,
            entity_id=appt.id,
            before=before,
            after=after,
        )
        appt.updated_by = user_id
        appt.updated_at = func.now()
        db.flush()
        if notify:
            from app.services import calendar_invites

            calendar_invites.on_update(db, appt, before, after)
    return appt


def delete_appointment(
    db: DbSession, appt: Appointment, *, user_id: int | None, notify: bool = True
) -> None:
    """Hard delete; the audit row keeps the full appointment (the email
    invite's CANCEL is built from it). The caller commits."""
    if notify:
        from app.services import calendar_invites

        calendar_invites.on_delete(db, appt)
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_DELETE,
        entity=ENTITY,
        entity_id=appt.id,
        before=audit.snapshot(appt, bookkeeping=True),
    )
    db.delete(appt)
    db.flush()


# ── Bitácora "Próxima cita" ─────────────────────────────────────────────────


def linked_to_entry(db: DbSession, entry_id: int) -> Appointment | None:
    return db.scalar(select(Appointment).where(Appointment.created_from_entry_id == entry_id))


def sync_next_appointment(
    db: DbSession,
    entry: LogEntry,
    data: dict[str, Any] | None,
    *,
    user_id: int | None,
) -> Appointment | None:
    """Create/update (``data``) or cancel (``None``) the entry's appointment.

    ``data`` has ``date`` and optionally ``time`` (absent or None: all day),
    ``doctor_id`` (absent: the entry's doctor) and ``location`` / ``reason``
    / ``notes`` (absent: unchanged). The entry must be flushed. Raises ``AppointmentError``; the caller commits or rolls back.
    """
    appt = linked_to_entry(db, entry.id)
    if appt is not None and appt.status == STATUS_DONE:
        raise AppointmentError(
            "La próxima cita de esta entrada ya se registró como realizada; no se puede cambiar."
        )
    if data is None:
        if appt is not None and appt.status == STATUS_SCHEDULED:
            update_appointment(db, appt, {"status": STATUS_CANCELLED}, user_id=user_id)
        return appt
    if data["date"] < entry.entry_date:
        raise AppointmentError("La próxima cita no puede ser anterior a la fecha de la entrada.")
    fields = {
        "patient_id": entry.patient_id,
        "doctor_id": data["doctor_id"] if "doctor_id" in data else entry.doctor_id,
        "starts_at": local_datetime(data["date"], data.get("time")),
        "all_day": data.get("time") is None,
    }
    # Text fields left out keep what the appointment has (e.g. a reason set
    # in the calendar); on a new appointment they start empty.
    fields.update({k: data[k] for k in ("location", "reason", "notes") if k in data})
    if appt is None:
        return create_appointment(db, fields, user_id=user_id, created_from_entry_id=entry.id)
    return update_appointment(db, appt, {**fields, "status": STATUS_SCHEDULED}, user_id=user_id)


def follow_entry_patient(db: DbSession, entry: LogEntry, *, user_id: int | None) -> None:
    """After an entry moves to another patient, its still-``programada``
    appointment follows it (a ``realizada`` / ``cancelada`` one is left alone)."""
    appt = linked_to_entry(db, entry.id)
    if appt is not None and appt.status == STATUS_SCHEDULED and appt.patient_id != entry.patient_id:
        update_appointment(db, appt, {"patient_id": entry.patient_id}, user_id=user_id)


# ── Listing ──────────────────────────────────────────────────────────────────


def filter_conditions(
    *,
    patient_ids: Sequence[int] | None = None,
    doctor_ids: Sequence[int] | None = None,
    statuses: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[ColumnElement[bool]]:
    """``date_from`` / ``date_to`` are inclusive days on the Panama clock."""
    conds: list[ColumnElement[bool]] = []
    if patient_ids:
        conds.append(Appointment.patient_id.in_(patient_ids))
    if doctor_ids:
        conds.append(Appointment.doctor_id.in_(doctor_ids))
    if statuses:
        conds.append(Appointment.status.in_(statuses))
    if date_from:
        conds.append(Appointment.starts_at >= local_datetime(date_from))
    if date_to:
        conds.append(Appointment.starts_at < local_datetime(date_to + timedelta(days=1)))
    return conds


def list_appointments(
    db: DbSession,
    conds: list[ColumnElement[bool]],
    *,
    order: str = "asc",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Appointment], int]:
    where = and_(*conds) if conds else None
    count_stmt = select(func.count()).select_from(Appointment)
    stmt = select(Appointment)
    if where is not None:
        count_stmt = count_stmt.where(where)
        stmt = stmt.where(where)
    total = db.scalar(count_stmt) or 0
    if order == "desc":
        stmt = stmt.order_by(Appointment.starts_at.desc(), Appointment.id.desc())
    else:
        stmt = stmt.order_by(Appointment.starts_at.asc(), Appointment.id.asc())
    return list(db.scalars(stmt.limit(limit).offset(offset))), total
