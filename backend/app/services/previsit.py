"""«Resumen pre-consulta» (DESIGN §6.6 ready-made actions, ticket #34).

:func:`previsit_summary` gathers, for one patient and one doctor, what the
family takes to the next visit with that doctor:

- **Period**: from the **last visit** — the most recent bitácora entry of
  the patient with that doctor whose type is a visit (``VISIT_TYPES``)
  dated **before** ``today`` — through ``today``, both inclusive (the last
  visit itself is listed: what the doctor said then). With no earlier visit
  the period is the last ``FALLBACK_DAYS`` days and ``last_visit`` is None.
- **Entries** in the period, of every doctor (the visit is about the
  patient, not only that doctor's notes), oldest first; the doctor's own are
  marked ``own``. Capped at ``MAX_ENTRIES`` (the newest are kept).
- **Medication changes** in the period: list versions whose date (the
  version's ``effective_date``, else the Panamá day it was saved) falls in
  it, with their change summary, and ``med_events`` dated in it.
- **Current medications** (today's list, printed-sheet order) and **EVITAR**.
- **Labs** in the period (report date = collected, else reported, else
  stored; ``failed`` reports left out) with every result and its flag: the
  newest ``MAX_LAB_REPORTS`` (shown oldest first; the PDF says when some
  were left out, and ``labs_total`` counts them all).
- **Upcoming appointments** (``programada``, from today on), the doctor's
  marked ``own``; at most ``MAX_APPOINTMENTS``.

Everything is copied into plain dataclasses, so the caller can give the
database connection back (``db.rollback()``) before rendering. The patient's
private fields are not read here: the PDF names the patient by display name.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session as DbSession

from app.models.doctor import Doctor
from app.models.lab import LabReport, LabResult
from app.models.log_entry import LogEntry
from app.models.medication import MedEvent, MedListVersion
from app.models.patient import Patient
from app.services import appointments as appointments_svc
from app.services import meds as meds_svc
from app.services.labs import reports as reports_svc

# Entry types that count as a visit with the doctor.
VISIT_TYPES = ("consulta_control", "consulta_enfermedad", "visita_domiciliaria", "teleconsulta")
FALLBACK_DAYS = 365
MAX_ENTRIES = 100
MAX_LAB_REPORTS = 30
MAX_APPOINTMENTS = 10
QUESTION_LINES = 12

ENTRY_TYPE_LABELS = {
    "consulta_control": "Control",
    "consulta_enfermedad": "Por enfermedad",
    "visita_domiciliaria": "Visita a domicilio",
    "teleconsulta": "Teleconsulta",
    "terapia": "Terapia",
    "vacuna": "Vacuna",
    "procedimiento": "Procedimiento",
    "hospitalizacion": "Hospitalización",
    "examen": "Examen",
    "observacion_familiar": "Observación familiar",
    "otro": "Otro",
}
EVENT_KIND_LABELS = {
    "inicio": "Inicio",
    "suspension": "Suspensión",
    "cambio_dosis": "Cambio de dosis",
    "sustitucion": "Sustitución",
}
FLAG_LABELS = {"alto": "Alto", "bajo": "Bajo", "anormal": "Anormal", "normal": "Normal"}


class PrevisitError(LookupError):
    """Unknown patient or doctor (404); the message is Spanish."""


@dataclass
class DoctorInfo:
    id: int
    name: str
    specialty: str | None = None
    phone: str | None = None
    clinic: str | None = None


@dataclass
class EntryRow:
    id: int
    date: date
    type_label: str
    who: str | None  # doctor name or source label
    title: str | None
    indications: str | None
    family_notes: str | None
    own: bool
    pending_review: bool


@dataclass
class MedChange:
    date: date
    text: str
    pending_review: bool = False


@dataclass
class MedRow:
    name: str
    strength: str | None
    quantity: str | None
    dose_note: str | None
    slot: str
    time: str | None
    instructions: str | None
    schedule_note: str | None
    days: list[int] | None = None  # ISO weekdays; None = daily


@dataclass
class AvoidRow:
    substance: str
    reason: str | None


@dataclass
class LabRow:
    panel: str | None
    name: str
    value: str | None
    unit: str | None
    ref: str | None
    flag: str | None  # alto / bajo / anormal / normal / None
    flag_label: str | None
    out_of_range: bool


@dataclass
class LabReportRow:
    id: int
    date: date
    lab_name: str | None
    pending_review: bool
    results: list[LabRow] = field(default_factory=list)

    @property
    def out_of_range(self) -> int:
        return sum(1 for r in self.results if r.out_of_range)


@dataclass
class AppointmentRow:
    date: date
    time: str | None
    doctor: str | None
    location: str | None
    reason: str | None
    own: bool


@dataclass
class PrevisitSummary:
    patient_id: int
    patient_slug: str
    patient_name: str  # display name (Papá / Mamá)
    doctor: DoctorInfo
    today: date
    period_from: date
    last_visit: date | None
    entries: list[EntryRow]
    entries_total: int
    med_changes: list[MedChange]
    meds: list[MedRow]
    avoid: list[AvoidRow]
    labs: list[LabReportRow]
    labs_total: int
    labs_out_of_range_total: int
    appointments: list[AppointmentRow]
    question_lines: int = QUESTION_LINES

    def counts(self) -> dict[str, int]:
        """Non-identifying counts (what the chat tool tells the model)."""
        return {
            "entries": self.entries_total,
            "med_changes": len(self.med_changes),
            "current_meds": len(self.meds),
            "avoid": len(self.avoid),
            "lab_reports": self.labs_total,
            "out_of_range_results": self.labs_out_of_range_total,
            "upcoming_appointments": len(self.appointments),
        }


def last_visit_date(db: DbSession, patient_id: int, doctor_id: int, today: date) -> date | None:
    """The date of the patient's latest visit-type entry with the doctor before ``today``."""
    return db.scalar(
        select(LogEntry.entry_date)
        .where(
            LogEntry.patient_id == patient_id,
            LogEntry.doctor_id == doctor_id,
            LogEntry.entry_type.in_(VISIT_TYPES),
            LogEntry.entry_date < today,
        )
        .order_by(LogEntry.entry_date.desc())
        .limit(1)
    )


def _num(value) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f") if hasattr(value, "normalize") else str(value)
    return text


def lab_value(result) -> str | None:
    value = result.value_text if result.value_text is not None else _num(result.value_num)
    if value is not None and result.comparator and not value.startswith(result.comparator):
        value = f"{result.comparator} {value}"
    return value


def lab_ref(result) -> str | None:
    if result.ref_text:
        return result.ref_text
    if result.ref_low is not None or result.ref_high is not None:
        low = _num(result.ref_low) or ""
        high = _num(result.ref_high) or ""
        return f"{low}–{high}"
    return None


def doctor_info(doctor: Doctor) -> DoctorInfo:
    return DoctorInfo(
        id=doctor.id,
        name=doctor.name,
        specialty=doctor.specialty,
        phone=doctor.phone,
        clinic=doctor.clinic,
    )


def _entries(db, patient_id, doctor_id, start, today) -> tuple[list[EntryRow], int]:
    where = and_(
        LogEntry.patient_id == patient_id,
        LogEntry.entry_date >= start,
        LogEntry.entry_date <= today,
    )
    rows = list(
        db.scalars(
            select(LogEntry)
            .where(where)
            .order_by(LogEntry.entry_date.desc(), LogEntry.entry_time.desc().nulls_last())
            .order_by(LogEntry.id.desc())
            .limit(MAX_ENTRIES + 1)
        )
    )
    total = len(rows)
    if total > MAX_ENTRIES:
        total = db.scalar(select(func.count()).select_from(LogEntry).where(where)) or 0
        rows = rows[:MAX_ENTRIES]
    out = [
        EntryRow(
            id=e.id,
            date=e.entry_date,
            type_label=ENTRY_TYPE_LABELS.get(e.entry_type, e.entry_type),
            who=e.doctor.name if e.doctor else e.source_label,
            title=e.title,
            indications=e.indications,
            family_notes=e.family_notes,
            own=e.doctor_id == doctor_id,
            pending_review=e.review_status != "ok",
        )
        for e in reversed(rows)
    ]
    return out, total


def _med_changes(db, patient_id, start, today) -> list[MedChange]:
    changes: list[MedChange] = []
    for v in db.scalars(
        select(MedListVersion)
        .where(MedListVersion.patient_id == patient_id)
        .order_by(MedListVersion.version_no)
    ):
        day = v.effective_date or appointments_svc.to_local(v.changed_at).date()
        if start <= day <= today:
            changes.append(
                MedChange(
                    date=day,
                    text=f"Lista de medicamentos (versión {v.version_no}): {v.change_summary}",
                )
            )
    for ev in db.scalars(
        select(MedEvent)
        .where(
            MedEvent.patient_id == patient_id,
            MedEvent.event_date >= start,
            MedEvent.event_date <= today,
        )
        .order_by(MedEvent.event_date, MedEvent.id)
    ):
        text = f"{EVENT_KIND_LABELS.get(ev.kind, ev.kind)}: {ev.medication}"
        if ev.detail:
            text += f" — {ev.detail}"
        changes.append(
            MedChange(date=ev.event_date, text=text, pending_review=ev.review_status != "ok")
        )
    changes.sort(key=lambda c: c.date)
    return changes


def current_meds(
    db: DbSession, patient_id: int, today: date
) -> tuple[list[MedRow], list[AvoidRow]]:
    meds = [
        MedRow(
            name=i.name,
            strength=i.strength,
            quantity=i.quantity,
            dose_note=i.dose_note,
            slot=i.slot,
            time=i.time.strftime("%H:%M") if i.time else None,
            instructions=i.instructions,
            schedule_note=i.schedule_note,
            days=list(i.days) if i.days else None,
        )
        for i in meds_svc.current_items(db, patient_id, today)
    ]
    avoid = [
        AvoidRow(substance=a.substance, reason=a.reason)
        for a in meds_svc.current_avoid(db, patient_id)
    ]
    return meds, avoid


def _labs(db, patient_id, start, today) -> tuple[list[LabReportRow], int, int]:
    """The period's reports (the newest ``MAX_LAB_REPORTS``, shown oldest
    first), how many there are in all and how many out-of-range results
    they have in all (shown or not)."""
    where = and_(
        LabReport.patient_id == patient_id,
        LabReport.status != "failed",
        reports_svc.report_date >= start,
        reports_svc.report_date <= today,
    )
    reports = list(
        db.scalars(
            select(LabReport)
            .where(where)
            .order_by(reports_svc.report_date.desc(), LabReport.id.desc())
            .limit(MAX_LAB_REPORTS + 1)
        )
    )
    total = len(reports)
    if total > MAX_LAB_REPORTS:
        total = db.scalar(select(func.count()).select_from(LabReport).where(where)) or 0
        reports = reports[:MAX_LAB_REPORTS]
    out_of_range = (
        db.scalar(
            select(func.count())
            .select_from(LabResult)
            .join(LabReport, LabReport.id == LabResult.report_id)
            .where(where, LabResult.flag.in_(reports_svc.OUT_OF_RANGE_FLAGS))
        )
        or 0
    )
    out = []
    for report in reversed(reports):
        row = LabReportRow(
            id=report.id,
            date=reports_svc.effective_date(report),
            lab_name=report.lab_name,
            pending_review=report.status != "ok",
        )
        for r in sorted(report.results, key=lambda r: (r.position, r.id)):
            row.results.append(
                LabRow(
                    panel=r.panel,
                    name=r.raw_name,
                    value=lab_value(r),
                    unit=r.unit,
                    ref=lab_ref(r),
                    flag=r.flag,
                    flag_label=FLAG_LABELS.get(r.flag) if r.flag else None,
                    out_of_range=r.flag in reports_svc.OUT_OF_RANGE_FLAGS,
                )
            )
        out.append(row)
    return out, total, out_of_range


def _appointments(db, patient_id, doctor_id, today) -> list[AppointmentRow]:
    conds = appointments_svc.filter_conditions(
        patient_ids=[patient_id], statuses=["programada"], date_from=today
    )
    rows, _ = appointments_svc.list_appointments(db, conds, limit=MAX_APPOINTMENTS)
    out = []
    for a in rows:
        local = appointments_svc.to_local(a.starts_at)
        out.append(
            AppointmentRow(
                date=local.date(),
                time=None if a.all_day else local.strftime("%H:%M"),
                doctor=a.doctor.name if a.doctor else None,
                location=a.location,
                reason=a.reason,
                own=a.doctor_id == doctor_id,
            )
        )
    return out


def get_patient(db: DbSession, patient_id: int) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise PrevisitError("Paciente no encontrado.")
    return patient


def previsit_summary(
    db: DbSession, patient_id: int, doctor_id: int, today: date | None = None
) -> PrevisitSummary:
    """The pre-visit summary data (rules in the module docstring).
    Raises :class:`PrevisitError` for an unknown patient or doctor."""
    today = today or appointments_svc.today_local()
    patient = get_patient(db, patient_id)
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise PrevisitError("Médico no encontrado.")
    last = last_visit_date(db, patient.id, doctor.id, today)
    start = last or today - timedelta(days=FALLBACK_DAYS)
    entries, total = _entries(db, patient.id, doctor.id, start, today)
    meds, avoid = current_meds(db, patient.id, today)
    labs, labs_total, labs_out_of_range = _labs(db, patient.id, start, today)
    return PrevisitSummary(
        patient_id=patient.id,
        patient_slug=patient.slug,
        patient_name=patient.display_name,
        doctor=doctor_info(doctor),
        today=today,
        period_from=start,
        last_visit=last,
        entries=entries,
        entries_total=total,
        med_changes=_med_changes(db, patient.id, start, today),
        meds=meds,
        avoid=avoid,
        labs=labs,
        labs_total=labs_total,
        labs_out_of_range_total=labs_out_of_range,
        appointments=_appointments(db, patient.id, doctor.id, today),
    )
