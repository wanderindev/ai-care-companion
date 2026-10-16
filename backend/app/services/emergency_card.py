"""«Tarjeta de emergencia» (DESIGN §6.6 ready-made actions, ticket #34).

:func:`emergency_card` gathers one patient's card: full name (else the
display name), ID number (CIP), birth date and age, diagnoses, today's
medications (one line per medication, its slots merged:
:func:`group_meds`), the EVITAR list and the patient's doctors.

**Doctors** are the active ones that appear in the patient's records — as
the doctor of a bitácora entry, of an appointment that is not cancelled, or
as the prescriber of a current medication — ranked by their most recent
reference (an upcoming appointment counts as its own date, a current
prescription as today), then by name; at most ``MAX_DOCTORS``.

This is the **one code path** that reads the patient's private fields
(``full_name``, ``id_number``, ``birth_date``) besides the printed
medication list: the card exists to carry them. The chat tool that renders
it never returns them to the model (only counts). The model has no
emergency-contact field, so the card has none.

Everything is copied into plain dataclasses, so the caller can give the
database connection back before rendering.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.appointment import Appointment
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.services import appointments as appointments_svc
from app.services import meds as meds_svc
from app.services import pdf as pdf_svc
from app.services.meds_diff import format_days
from app.services.pdf.meds import PRINT_LABELS
from app.services.previsit import (
    AvoidRow,
    DoctorInfo,
    MedRow,
    PrevisitError,
    current_meds,
    doctor_info,
    get_patient,
)

MAX_DOCTORS = 8
MAX_INSTRUCTIONS_CHARS = 60
NO_QUANTITY = "cantidad según indicación"


@dataclass
class MedGroup:
    """One medication on the card: its rows (one per slot) merged, e.g.
    «Losartán 50 mg» — «½ Desayuno», «½ Cena»."""

    name: str
    doses: list[str] = field(default_factory=list)


def _squash(text: str | None) -> str | None:
    return (" ".join(text.split()) or None) if text else None


def dose_text(m: MedRow) -> str:
    """One row as a dose: «½ Desayuno 07:00», then its day rule, schedule
    note and short instructions, so a weekly or tapering drug never reads as
    daily: «1 Desayuno — lun (semanal, los lunes)». No quantity → the slot
    plus «cantidad según indicación»."""
    quantity = pdf_svc.format_quantity(m.quantity)
    head = " ".join(p for p in (quantity, PRINT_LABELS.get(m.slot, m.slot), m.time) if p)
    if not quantity:
        head += f", {NO_QUANTITY}"
    notes = []
    schedule = _squash(m.schedule_note)
    if schedule:
        notes.append(schedule)
    instructions = _squash(m.instructions)
    if instructions and len(instructions) <= MAX_INSTRUCTIONS_CHARS:
        notes.append(instructions)
    days = format_days(m.days) if m.days else None
    tail = days or ""
    if notes:
        tail = f"{tail} ({'; '.join(notes)})" if tail else "; ".join(notes)
    return f"{head} — {tail}" if tail else head


def group_meds(meds: list[MedRow]) -> list[MedGroup]:
    """Rows with the same name + strength + dose note become one group, in
    printed-sheet order, with one dose per row (:func:`dose_text`). Identical
    rows stay two doses: merging them would understate the dose."""
    groups: dict[str, MedGroup] = {}
    for m in meds:
        name = " ".join(p for p in (m.name, m.strength, m.dose_note) if p)
        group = groups.setdefault(name.casefold(), MedGroup(name=name))
        group.doses.append(dose_text(m))
    return list(groups.values())


@dataclass
class EmergencyCard:
    patient_id: int
    patient_slug: str
    display_name: str
    full_name: str | None
    id_number: str | None
    birth_date: date | None
    age: int | None
    diagnoses: str | None
    meds: list[MedRow]
    avoid: list[AvoidRow]
    doctors: list[DoctorInfo]
    today: date
    med_groups: list[MedGroup] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Non-identifying counts (what the chat tool tells the model)."""
        return {
            "current_meds": len(self.meds),
            "avoid": len(self.avoid),
            "doctors": len(self.doctors),
        }


def age_on(birth: date | None, today: date) -> int | None:
    if birth is None or birth > today:
        return None
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def ranked_doctors(db: DbSession, patient_id: int, today: date, limit: int = MAX_DOCTORS):
    """The patient's doctors by most recent reference (see module docstring)."""
    latest: dict[int, date] = {}

    def see(doctor_id: int | None, day: date | None) -> None:
        if doctor_id is None or day is None:
            return
        if doctor_id not in latest or day > latest[doctor_id]:
            latest[doctor_id] = day

    for doctor_id, day in db.execute(
        select(LogEntry.doctor_id, func.max(LogEntry.entry_date))
        .where(LogEntry.patient_id == patient_id, LogEntry.doctor_id.is_not(None))
        .group_by(LogEntry.doctor_id)
    ):
        see(doctor_id, day)
    for doctor_id, starts_at in db.execute(
        select(Appointment.doctor_id, func.max(Appointment.starts_at))
        .where(
            Appointment.patient_id == patient_id,
            Appointment.doctor_id.is_not(None),
            Appointment.status != "cancelada",
        )
        .group_by(Appointment.doctor_id)
    ):
        see(doctor_id, appointments_svc.to_local(starts_at).date())
    for item in meds_svc.current_items(db, patient_id, today):
        see(item.prescriber_id, today)
    if not latest:
        return []
    doctors = db.scalars(
        select(Doctor).where(Doctor.id.in_(latest), Doctor.is_active.is_(True))
    ).all()
    doctors = sorted(doctors, key=lambda d: d.name.lower())
    doctors.sort(key=lambda d: latest[d.id], reverse=True)
    return [doctor_info(d) for d in doctors[:limit]]


def emergency_card(db: DbSession, patient_id: int, today: date | None = None) -> EmergencyCard:
    """The card data. Raises :class:`PrevisitError` (404) for an unknown patient."""
    today = today or appointments_svc.today_local()
    patient = get_patient(db, patient_id)
    meds, avoid = current_meds(db, patient.id, today)
    return EmergencyCard(
        patient_id=patient.id,
        patient_slug=patient.slug,
        display_name=patient.display_name,
        full_name=(patient.full_name or "").strip() or None,
        id_number=(patient.id_number or "").strip() or None,
        birth_date=patient.birth_date,
        age=age_on(patient.birth_date, today),
        diagnoses=(patient.diagnoses or "").strip() or None,
        meds=meds,
        avoid=avoid,
        doctors=ranked_doctors(db, patient.id, today),
        today=today,
        med_groups=group_meds(meds),
    )


__all__ = ["EmergencyCard", "PrevisitError", "emergency_card", "ranked_doctors", "age_on"]
