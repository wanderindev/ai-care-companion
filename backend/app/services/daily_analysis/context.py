"""The record context sent with every analysis call (DESIGN §6.7 step 4).

Built once per job and identical for every day the job analyzes, so after the
first call it is read from the prompt cache. Compact text, one line per
record, with the ids the model must quote back (doctor, entry, medication
item):

- patients: slug, display name, diagnoses;
- doctors (active): id, name, aliases, specialty;
- current medications per patient: item id, exact name, strength, quantity,
  slot, time;
- bitácora per patient: the last ``RECENT_ENTRIES`` entries plus every entry
  within ``WINDOW_DAYS`` of the analyzed days (id, date, doctor, type, title,
  a short summary);
- appointments (``programada``) from the first analyzed day on (backfill:
  from today on), per patient at most ``UPCOMING_APPOINTMENTS``.

PHI: the text is medical; it goes only to the Anthropic API (DESIGN §8) and
is never logged.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from app.models.appointment import Appointment
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.models.medication import MedItem
from app.models.patient import Patient
from app.services import meds
from app.services.appointments import local_datetime, to_local

RECENT_ENTRIES = 30
WINDOW_DAYS = 7
UPCOMING_APPOINTMENTS = 20
SUMMARY_CHARS = 160


@dataclass
class AnalysisContext:
    """The context text plus what the code needs to check the answer."""

    text: str
    patients: dict[str, int] = field(default_factory=dict)  # slug → id
    doctor_ids: set[int] = field(default_factory=set)
    med_items: dict[int, MedItem] = field(default_factory=dict)  # current items by id
    med_versions: dict[int, int] = field(default_factory=dict)  # patient id → version_no


def _clean(value: str | None, limit: int | None = None) -> str:
    text = " ".join((value or "").split())
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _doctor_label(doctor: Doctor | None) -> str:
    return f"{doctor.name} (id {doctor.id})" if doctor else "sin médico"


def build_context(db: DbSession, days: list[date], *, today: date | None = None) -> AnalysisContext:
    """The context for analyzing ``days`` (sorted, at least one).

    ``today`` (backfill): the medication list and the scheduled appointments
    are those as of that day instead of the first analyzed day (the run
    compares old messages with today's list)."""
    first, last = min(days), max(days)
    as_of = today or first
    ctx = AnalysisContext(text="")
    lines: list[str] = ["CONTEXTO DE LA APP", "", "PACIENTES"]
    patients = list(db.scalars(select(Patient).order_by(Patient.id)))
    for p in patients:
        ctx.patients[p.slug] = p.id
        diagnoses = _clean(p.diagnoses, 600)
        lines.append(
            f"- {p.slug}: {p.display_name}" + (f". Diagnósticos: {diagnoses}" if diagnoses else "")
        )

    lines += ["", "MÉDICOS (id · nombre · alias · especialidad)"]
    doctors = list(
        db.scalars(select(Doctor).where(Doctor.is_active.is_(True)).order_by(Doctor.name))
    )
    for d in doctors:
        ctx.doctor_ids.add(d.id)
        aliases = ", ".join(a for a in (d.aliases or []) if a)
        lines.append(f"- {d.id} · {d.name} · {aliases or '-'} · {d.specialty or '-'}")
    if not doctors:
        lines.append("- (ninguno)")

    for p in patients:
        version = meds.latest_version(db, p.id)
        ctx.med_versions[p.id] = version.version_no if version else 0
        lines += [
            "",
            f"MEDICAMENTOS ACTUALES DE {p.display_name} ({p.slug}) "
            "(item_id · nombre exacto · concentración · cantidad · momento · hora)",
        ]
        items = meds.current_items(db, p.id, today=as_of)
        for item in items:
            ctx.med_items[item.id] = item
            at = item.time.strftime("%H:%M") if item.time else "-"
            lines.append(
                f"- {item.id} · {item.name} · {item.strength or '-'} · {item.quantity or '-'} "
                f"· {item.slot} · {at}"
            )
        if not items:
            lines.append("- (ninguno)")

    lo, hi = first - timedelta(days=WINDOW_DAYS), last + timedelta(days=WINDOW_DAYS)
    for p in patients:
        recent = select(LogEntry.id).where(LogEntry.patient_id == p.id)
        recent = recent.order_by(LogEntry.entry_date.desc(), LogEntry.id.desc())
        recent_ids = list(db.scalars(recent.limit(RECENT_ENTRIES)))
        entries = list(
            db.scalars(
                select(LogEntry)
                .where(
                    LogEntry.patient_id == p.id,
                    or_(
                        LogEntry.id.in_(recent_ids),
                        LogEntry.entry_date.between(lo, hi),
                    ),
                )
                .order_by(LogEntry.entry_date, LogEntry.id)
            )
        )
        lines += [
            "",
            f"BITÁCORA DE {p.display_name} ({p.slug}) "
            "(entry_id · fecha · médico · tipo · título · resumen)",
        ]
        for e in entries:
            title = _clean(e.title or e.source_label, 120) or "-"
            summary = _clean(e.indications or e.family_notes, SUMMARY_CHARS) or "-"
            lines.append(
                f"- {e.id} · {e.entry_date.isoformat()} · {_doctor_label(e.doctor)} "
                f"· {e.entry_type} · {title} · {summary}"
            )
        if not entries:
            lines.append("- (ninguna)")

    lines += ["", "CITAS PROGRAMADAS (paciente · fecha y hora de Panamá · médico · motivo)"]
    any_appt = False
    for p in patients:
        appts = db.scalars(
            select(Appointment)
            .where(
                Appointment.patient_id == p.id,
                Appointment.status == "programada",
                Appointment.starts_at >= local_datetime(as_of),
            )
            .order_by(Appointment.starts_at, Appointment.id)
            .limit(UPCOMING_APPOINTMENTS)
        )
        for a in appts:
            any_appt = True
            local = to_local(a.starts_at)
            when = local.date().isoformat() if a.all_day else local.strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"- {p.slug} · {when} · {_doctor_label(a.doctor)} · {_clean(a.reason, 120) or '-'}"
            )
    if not any_appt:
        lines.append("- (ninguna)")

    ctx.text = "\n".join(lines)
    return ctx
