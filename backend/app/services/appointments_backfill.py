"""Backfill appointments from imported bitácora entries (operator CLI:
``python -m scripts.appointments backfill``).

The DOCX importer keeps each entry's "Próxima cita" in
``log_entries.origin_ref.next_appointment`` (``{date, time, doctor, text}``,
see ``bitacora_import.extract``). For every such entry with a date this
creates one appointment linked by ``created_from_entry_id`` (unique, so
re-running skips the entries already done):

- ``starts_at``: the date at ``time`` (Panama), or all day when there is no time.
- doctor: the named doctor when it matches exactly one in the directory
  (``match_doctor``); no name → the entry's own doctor; a name that matches
  none or several → no doctor (counted). ``notes`` keeps the phrase as written.
- status: an appointment on or before today is ``realizada`` when a later
  entry of the same patient **with the same doctor** exists on that day (or
  one day either side, the closest wins), which becomes its
  ``result_entry_id`` (each entry is the result of at most one appointment).
  Without such a match it stays ``programada``, past or not: nobody recorded
  the visit, and the calendar shows a past ``programada`` as unconfirmed
  rather than guessing it was cancelled. Future ones are ``programada``.
- skipped: no date or an unreadable one, or a date before the entry's own
  (an extraction slip). Counted, never printed.
- duplicates: logs often repeat the same "Próxima cita" in several entries
  (a consult and a later lab entry both say "Próxima cita 5 MAR"). The
  later entry gets **no** appointment of its own (``created_from_entry_id``
  is unique, so it cannot share the existing one; counted as
  ``duplicate_same_day``) when the patient already has one that Panamá day
  (from an earlier entry in this run, a previous run, the app, or one
  deleted in the app; a cancelled one counts too) that is the same visit
  (``Slot.same_visit``): times not both given and different, and the same
  doctor; a side with no doctor at all (none named, none on the entry)
  fits any doctor, but a named doctor the directory does not know is not a
  wildcard: it only matches the same unknown name.
- deleted: an entry whose backfilled (or form-created) appointment was later
  deleted in the app (an audit ``delete`` row naming it as
  ``created_from_entry_id``) is skipped, and the deleted appointment's slot
  stays occupied, so a re-run never brings back what someone removed, not
  even through another entry repeating it (``deleted_before``).

No email invites: appointments are created with ``notify=False``, so the
operator's run never emails the family about historic appointments
(``app.services.calendar_invites``).

Counts only: nothing here logs or prints medical text.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.appointment import STATUS_DONE, STATUS_SCHEDULED, Appointment
from app.models.audit import AuditLog
from app.models.log_entry import LogEntry
from app.services.appointments import (
    AppointmentError,
    create_appointment,
    local_datetime,
    to_local,
    today_local,
)
from app.services.doctors import match_doctor, normalize_name

MATCH_SLACK = timedelta(days=1)


@dataclass
class BackfillReport:
    candidates: int = 0  # entries with an origin_ref.next_appointment
    existing: int = 0  # already have their appointment
    no_date: int = 0
    before_entry: int = 0
    duplicate_same_day: int = 0  # same patient/day/doctor already has one
    deleted_before: int = 0  # its appointment was deleted in the app
    created: int = 0
    done: int = 0  # created as realizada, with result_entry_id
    past_open: int = 0  # past (or today), no matching entry: programada
    upcoming: int = 0  # future: programada
    doctor_unmatched: int = 0  # named a doctor not in the directory (incl. duplicates)
    failed: int = 0


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    try:
        return time.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _doctor_id(db: DbSession, entry: LogEntry, name: Any) -> tuple[int | None, bool]:
    """``(doctor_id, unmatched)`` for the doctor named in the next appointment."""
    if not isinstance(name, str) or not normalize_name(name):
        return entry.doctor_id, False
    doctor = match_doctor(db, name)
    return (doctor.id, False) if doctor else (None, True)


def find_result_entry(
    db: DbSession, source: LogEntry, day: date, doctor_id: int | None, used: set[int]
) -> LogEntry | None:
    """The entry recording the visit: same patient and doctor, written after
    ``source``, on ``day`` ± ``MATCH_SLACK`` (closest first), not yet used."""
    if doctor_id is None:
        return None
    rows = db.scalars(
        select(LogEntry)
        .where(
            LogEntry.patient_id == source.patient_id,
            LogEntry.doctor_id == doctor_id,
            LogEntry.id != source.id,
            LogEntry.entry_date > source.entry_date,
            LogEntry.entry_date >= day - MATCH_SLACK,
            LogEntry.entry_date <= day + MATCH_SLACK,
        )
        .order_by(LogEntry.entry_date, LogEntry.id)
    ).all()
    rows = [r for r in rows if r.id not in used]
    if not rows:
        return None
    return min(rows, key=lambda r: abs((r.entry_date - day).days))


@dataclass(frozen=True)
class Slot:
    """What the dedupe compares: an appointment (live, deleted or about to be
    created) reduced to patient, Panamá day, time, doctor and, when the phrase
    named a doctor the directory does not know, that name normalized."""

    patient_id: int
    day: date
    at: time | None  # None: all day / unknown time
    doctor_id: int | None
    name: str  # normalized unmatched doctor name; "" when none was named

    @property
    def wildcard(self) -> bool:
        """No doctor at all (none named, none on the entry): any doctor fits."""
        return self.doctor_id is None and not self.name

    def same_visit(self, other: "Slot") -> bool:
        if (self.patient_id, self.day) != (other.patient_id, other.day):
            return False
        if self.at is not None and other.at is not None and self.at != other.at:
            return False
        if self.doctor_id is not None and other.doctor_id is not None:
            return self.doctor_id == other.doctor_id
        if self.wildcard or other.wildcard:
            return True
        # Neither is a wildcard and at least one has no doctor id: the same
        # only when both named the same unknown doctor.
        return self.doctor_id is None and other.doctor_id is None and self.name == other.name


def _named(entry: LogEntry | None) -> str:
    """Normalized doctor name an imported entry's "Próxima cita" gives."""
    if entry is None:
        return ""
    next_appt = (entry.origin_ref or {}).get("next_appointment")
    name = next_appt.get("doctor") if isinstance(next_appt, dict) else None
    return normalize_name(name) if isinstance(name, str) else ""


def _slot(
    patient_id: int,
    starts_at: datetime,
    all_day: bool,
    doctor_id: int | None,
    source: LogEntry | None,
) -> Slot:
    local = to_local(starts_at)
    return Slot(
        patient_id=patient_id,
        day=local.date(),
        at=None if all_day else local.time(),
        doctor_id=doctor_id,
        name="" if doctor_id is not None else _named(source),
    )


def occupied_slots(db: DbSession) -> tuple[list[Slot], set[int]]:
    """Slots of every appointment in the table (any status: a cancelled one
    still occupies its day) and of every appointment deleted in the app (from
    the audit snapshot), plus the entries whose appointment was deleted."""
    slots: list[Slot] = []
    for appt in db.scalars(select(Appointment)):
        source = (
            db.get(LogEntry, appt.created_from_entry_id) if appt.created_from_entry_id else None
        )
        slots.append(_slot(appt.patient_id, appt.starts_at, appt.all_day, appt.doctor_id, source))
    deleted_entries: set[int] = set()
    for before in db.scalars(
        select(AuditLog.before).where(AuditLog.entity == "appointment", AuditLog.action == "delete")
    ):
        if not isinstance(before, dict):
            continue
        source_id = before.get("created_from_entry_id")
        if isinstance(source_id, int):
            deleted_entries.add(source_id)
        try:
            starts_at = datetime.fromisoformat(before["starts_at"])
            patient_id = int(before["patient_id"])
        except (KeyError, TypeError, ValueError):
            continue
        source = db.get(LogEntry, source_id) if isinstance(source_id, int) else None
        slots.append(
            _slot(
                patient_id,
                starts_at,
                bool(before.get("all_day")),
                before.get("doctor_id"),
                source,
            )
        )
    return slots, deleted_entries


def backfill(db: DbSession, *, user_id: int | None, today: date | None = None) -> BackfillReport:
    """Create the missing appointments in the caller's transaction (the caller
    commits, or rolls back for a dry run)."""
    today = today or today_local()
    report = BackfillReport()
    existing = set(
        db.scalars(
            select(Appointment.created_from_entry_id).where(
                Appointment.created_from_entry_id.is_not(None)
            )
        )
    )
    used = set(
        db.scalars(
            select(Appointment.result_entry_id).where(Appointment.result_entry_id.is_not(None))
        )
    )
    slots, deleted = occupied_slots(db)
    entries = db.scalars(
        select(LogEntry)
        .where(LogEntry.origin_ref.has_key("next_appointment"))
        .order_by(LogEntry.entry_date, LogEntry.id)
    ).all()
    for entry in entries:
        next_appt = (entry.origin_ref or {}).get("next_appointment")
        if not isinstance(next_appt, dict):
            continue
        report.candidates += 1
        if entry.id in existing:
            report.existing += 1
            continue
        if entry.id in deleted:
            report.deleted_before += 1
            continue
        day = _parse_date(next_appt.get("date"))
        if day is None:
            report.no_date += 1
            continue
        if day < entry.entry_date:
            report.before_entry += 1
            continue
        at = _parse_time(next_appt.get("time"))
        doctor_id, unmatched = _doctor_id(db, entry, next_appt.get("doctor"))
        report.doctor_unmatched += unmatched
        slot = Slot(
            patient_id=entry.patient_id,
            day=day,
            at=at,
            doctor_id=doctor_id,
            name=_named(entry) if doctor_id is None else "",
        )
        if any(slot.same_visit(other) for other in slots):
            report.duplicate_same_day += 1
            continue
        result = find_result_entry(db, entry, day, doctor_id, used) if day <= today else None
        text = next_appt.get("text")
        fields = {
            "patient_id": entry.patient_id,
            "doctor_id": doctor_id,
            "starts_at": local_datetime(day, at),
            "all_day": at is None,
            "notes": text.strip() if isinstance(text, str) and text.strip() else None,
        }
        try:
            with db.begin_nested():
                create_appointment(
                    db,
                    fields,
                    user_id=user_id,
                    status=STATUS_DONE if result else STATUS_SCHEDULED,
                    created_from_entry_id=entry.id,
                    result_entry_id=result.id if result else None,
                    notify=False,  # historic: never email the family
                )
        except AppointmentError:
            report.failed += 1
            continue
        slots.append(slot)
        report.created += 1
        if result is not None:
            used.add(result.id)
            report.done += 1
        elif day <= today:
            report.past_open += 1
        else:
            report.upcoming += 1
    return report
