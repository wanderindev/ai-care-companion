"""Chat tools: definitions sent to Claude and their read-only handlers.

Each :class:`Tool` has the API definition (name, description, JSON schema), a
short Spanish status for the UI («Buscando en la bitácora…») and a handler
``handler(ctx, args) -> JSON-able``. The engine runs every call in its own
short database session (``ctx.db``) and turns the result into a
``tool_result`` block with :func:`run_tool`.

Rules for handlers:

- **Read-only**, except the file tools (``generate_pdf``,
  ``generate_xlsx``, :mod:`app.services.chat.files`, and the ready-made
  ``previsit_summary`` / ``emergency_card`` of #34): they store the file
  as an *unlinked* attachment (committed at once), register it in
  ``ctx.attachment_ids`` / ``ctx.files``, and the engine announces it (SSE
  ``file``) and links it to the turn's final assistant message when it
  stores the turn. They give the database connection back while rendering.
  And ``propose_change`` (#41), which never writes a record: it validates
  the proposal (the target API's schema via
  ``suggestions.parse_payload``, existing doctors, no future bitácora date),
  refuses duplicates of records or pending suggestions
  (``daily_analysis.pipeline.find_duplicate``, the WhatsApp analysis'
  check), then adds a ``pending`` suggestion with
  ``suggestions.create_suggestion`` (``source="chat"``, the session, the
  asking family member as ``created_by``) and commits at once. It registers
  it in ``ctx.suggestions``; the engine announces it (SSE ``suggestion``)
  and lists its id in the final assistant message's
  ``meta.suggestion_ids``.
- **Patient-aware**: ``patient`` takes the slug (``papa`` / ``mama``) or the
  numeric id; :func:`_patient` resolves it.
- **Compact**: short keys, ``None`` values dropped, dates ISO, ids
  everywhere so the answer can cite an entry / report / appointment. Lists
  are paginated (``limit`` / ``offset`` with ``total``) and every result is
  capped at ``MAX_RESULT_CHARS`` (items are dropped from the end and the
  result says so).
- Bad input is a :class:`ToolInputError`: the model gets an ``is_error``
  result with the message and can retry.

Private patient fields: ``list_patients`` returns display names, diagnoses
and notes — the clinical context the answers need. ``full_name``,
``id_number`` (CIP) and ``birth_date`` are never exposed to the chat. The
``emergency_card`` tool renders the card server-side (its own code path,
:mod:`app.services.emergency_card`): the PDF carries the full name, CIP and
birth date because that is what the card is for, and it lands in the
family's own chat attachments; the tool result Claude sees has only the
file id, the file name (slug + date) and counts.

The tool list is static (same order, same text): it is part of the cached
prefix and of what a replayed thinking block is bound to. Adding
``propose_change`` (#41) changed it once: earlier sessions' replayed
thinking blocks are dropped by the API (``drop_block``) and the cache is
rebuilt on their next turn.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.models.appointment import STATUSES as APPOINTMENT_STATUSES
from app.models.appointment import Appointment
from app.models.doctor import Doctor
from app.models.lab import LabReport, LabResult
from app.models.log_entry import ENTRY_TYPES, LogEntry
from app.models.patient import Patient
from app.services import appointments as appointments_svc
from app.services import attachments as attachments_svc
from app.services import emergency_card as card_svc
from app.services import ledd_substances
from app.services import log_entries as log_svc
from app.services import meds as meds_svc
from app.services import pdf as pdf_svc
from app.services import previsit as previsit_svc
from app.services import suggestions as suggestions_svc
from app.services.chat import files
from app.services.daily_analysis import pipeline
from app.services.doctors import match_doctors, strip_accents
from app.services.labs import reports as reports_svc
from app.services.pdf import summaries as summaries_pdf
from app.services.pdf.chat_document import ChatDocument, render_chat_document
from app.services.storage import StorageNotConfigured

MAX_RESULT_CHARS = 24_000
EXCERPT_CHARS = 280
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
logger = logging.getLogger(__name__)

TRUNCATED_NOTE = (
    "Resultado recortado por tamaño: hay más elementos. Usa filtros más específicos "
    "o pagina con offset."
)


class ToolInputError(ValueError):
    """Invalid arguments; the message goes back to the model as an error result."""


@dataclass
class ToolContext:
    db: DbSession
    user_id: int | None = None
    session_id: int | None = None
    # ``storage()`` returns the attachments' Storage (None: files cannot be stored).
    storage: Callable[[], Any] | None = None
    attachment_ids: list[int] = field(default_factory=list)
    # Files this call created, as announced to the UI: {id, filename, mime, size}.
    files: list[dict[str, Any]] = field(default_factory=list)
    # Suggestions this call created (#41): {id, kind, patient_id, summary}.
    suggestions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    status: str
    handler: Callable[[ToolContext, dict[str, Any]], Any]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ── Argument helpers ─────────────────────────────────────────────────────────


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_PATIENT = {
    "type": "string",
    "description": 'Paciente: "papa" o "mama" (o su id numérico).',
}
_DATE = {"type": "string", "description": "Fecha AAAA-MM-DD."}
_LIMIT = {
    "type": "integer",
    "description": f"Máximo de resultados (1–{MAX_LIMIT}, por defecto {DEFAULT_LIMIT}).",
}
_OFFSET = {"type": "integer", "description": "Saltar los primeros N resultados (paginación)."}
_CELL = {"type": ["string", "number", "boolean", "null"]}
_FILENAME = {
    "type": "string",
    "description": "Nombre corto del archivo, sin extensión (se le añade la fecha).",
}


def _patient(db: DbSession, value: Any, *, required: bool = False) -> Patient | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ToolInputError('Falta "patient": usa "papa" o "mama".')
        return None
    text = str(value).strip().lower()
    if text.isdigit():
        patient = db.get(Patient, int(text))
    else:
        patient = db.scalar(select(Patient).where(Patient.slug == text))
    if patient is None:
        raise ToolInputError(f'Paciente desconocido: "{value}". Usa "papa" o "mama".')
    return patient


def _date(value: Any, name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ToolInputError(f'"{name}" debe ser una fecha AAAA-MM-DD.') from None


def _int(value: Any, name: str, *, default: int | None = None, low: int = 0, high: int = 10**9):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ToolInputError(f'"{name}" debe ser un número entero.')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolInputError(f'"{name}" debe ser un número entero.') from None
    return max(low, min(high, number))


def _str(value: Any, name: str, max_len: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolInputError(f'"{name}" debe ser texto.')
    value = value.strip()
    return value[:max_len] or None


def _page(args: dict[str, Any]) -> tuple[int, int]:
    limit = _int(args.get("limit"), "limit", default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
    offset = _int(args.get("offset"), "offset", default=0, low=0)
    return limit, offset


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None and v != [] and v != ""}


def _excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str | None:
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _slugs(db: DbSession) -> dict[int, str]:
    return {p.id: p.slug for p in db.scalars(select(Patient))}


def _doctor_names(db: DbSession, ids: set[int | None]) -> dict[int, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return dict(db.execute(select(Doctor.id, Doctor.name).where(Doctor.id.in_(wanted))).all())


# ── Handlers ─────────────────────────────────────────────────────────────────


def list_patients(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = ctx.db.scalars(select(Patient).order_by(Patient.id))
    return [
        _compact(
            {
                "id": p.id,
                "patient": p.slug,
                "name": p.display_name,
                "diagnoses": p.diagnoses,
                "notes": p.notes,
            }
        )
        for p in rows
    ]


def list_doctors(ctx: ToolContext, args: dict[str, Any]) -> Any:
    q = _str(args.get("q"), "q")
    include_inactive = bool(args.get("include_inactive"))
    stmt = select(Doctor).order_by(Doctor.name)
    if not include_inactive:
        stmt = stmt.where(Doctor.is_active.is_(True))
    if q:
        folded = ctx.db.scalar(select(func.lower(func.immutable_unaccent(q))))
        pattern = f"%{folded}%"

        def fold(column):
            return func.lower(func.immutable_unaccent(column))

        stmt = stmt.where(
            or_(
                fold(Doctor.name).like(pattern),
                fold(func.array_to_string(Doctor.aliases, " | ")).like(pattern),
                fold(func.coalesce(Doctor.specialty, "")).like(pattern),
            )
        )
    return [
        _compact(
            {
                "id": d.id,
                "name": d.name,
                "aliases": list(d.aliases or []),
                "specialty": d.specialty,
                "clinic": d.clinic,
                "phone": d.phone,
                "email": d.email,
                "address": d.address,
                "notes": _excerpt(d.notes),
                "inactive": True if not d.is_active else None,
            }
        )
        for d in ctx.db.scalars(stmt.limit(200))
    ]


def search_log_entries(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    patient = _patient(db, args.get("patient"))
    entry_type = _str(args.get("entry_type"), "entry_type")
    if entry_type and entry_type not in ENTRY_TYPES:
        raise ToolInputError(f'"entry_type" debe ser uno de: {", ".join(ENTRY_TYPES)}.')
    doctor_id = _int(args.get("doctor_id"), "doctor_id")
    limit, offset = _page(args)
    conds = log_svc.filter_conditions(
        db,
        patient_ids=[patient.id] if patient else None,
        doctor_ids=[doctor_id] if doctor_id else None,
        entry_types=[entry_type] if entry_type else None,
        date_from=_date(args.get("date_from"), "date_from"),
        date_to=_date(args.get("date_to"), "date_to"),
        q=_str(args.get("q"), "q"),
    )
    order = "asc" if args.get("order") == "asc" else "desc"
    rows, total = log_svc.list_entries(db, conds, order=order, limit=limit, offset=offset)
    slugs = _slugs(db)
    return {
        "total": total,
        "offset": offset,
        "items": [
            _compact(
                {
                    "id": e.id,
                    "date": _iso(e.entry_date),
                    "time": e.entry_time.strftime("%H:%M") if e.entry_time else None,
                    "patient": slugs.get(e.patient_id),
                    "doctor": e.doctor.name if e.doctor else None,
                    "doctor_id": e.doctor_id,
                    "source": e.source_label,
                    "type": e.entry_type,
                    "channel": e.channel,
                    "title": e.title,
                    "indications": _excerpt(e.indications),
                    "family_notes": _excerpt(e.family_notes),
                    "pending_review": True if e.review_status != "ok" else None,
                }
            )
            for e in rows
        ],
    }


def get_log_entry(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    entry_id = _int(args.get("id"), "id")
    entry = db.get(LogEntry, entry_id) if entry_id else None
    if entry is None:
        raise ToolInputError(f"No existe la entrada {args.get('id')}.")
    files = log_svc.attachments_by_entry(db, [entry.id])[entry.id]
    appt = appointments_svc.linked_to_entry(db, entry.id)
    return _compact(
        {
            "id": entry.id,
            "date": _iso(entry.entry_date),
            "time": entry.entry_time.strftime("%H:%M") if entry.entry_time else None,
            "patient": _slugs(db).get(entry.patient_id),
            "doctor": entry.doctor.name if entry.doctor else None,
            "doctor_id": entry.doctor_id,
            "source": entry.source_label,
            "type": entry.entry_type,
            "channel": entry.channel,
            "title": entry.title,
            "indications": entry.indications,
            "family_notes": entry.family_notes,
            "pending_review": True if entry.review_status != "ok" else None,
            "files": [{"id": a.id, "name": a.filename} for a in files],
            "next_appointment": _appointment(appt, {}) if appt else None,
        }
    )


def _appointment(appt: Appointment, slugs: dict[int, str]) -> dict[str, Any]:
    local = appointments_svc.to_local(appt.starts_at)
    return _compact(
        {
            "id": appt.id,
            "date": local.date().isoformat(),
            "time": None if appt.all_day else local.strftime("%H:%M"),
            "patient": slugs.get(appt.patient_id),
            "doctor": appt.doctor.name if appt.doctor else None,
            "doctor_id": appt.doctor_id,
            "status": appt.status,
            "location": appt.location,
            "reason": appt.reason,
            "notes": _excerpt(appt.notes),
            "from_entry_id": appt.created_from_entry_id,
            "result_entry_id": appt.result_entry_id,
        }
    )


def list_appointments(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    patient = _patient(db, args.get("patient"))
    status = _str(args.get("status"), "status")
    if status and status not in APPOINTMENT_STATUSES:
        raise ToolInputError(f'"status" debe ser uno de: {", ".join(APPOINTMENT_STATUSES)}.')
    date_from = _date(args.get("date_from"), "date_from")
    date_to = _date(args.get("date_to"), "date_to")
    if date_from is None and date_to is None:
        date_from = appointments_svc.today_local()  # default: upcoming
    doctor_id = _int(args.get("doctor_id"), "doctor_id")
    limit, offset = _page(args)
    conds = appointments_svc.filter_conditions(
        patient_ids=[patient.id] if patient else None,
        doctor_ids=[doctor_id] if doctor_id else None,
        statuses=[status] if status else None,
        date_from=date_from,
        date_to=date_to,
    )
    order = "desc" if args.get("order") == "desc" else "asc"
    rows, total = appointments_svc.list_appointments(
        db, conds, order=order, limit=limit, offset=offset
    )
    slugs = _slugs(db)
    return {
        "total": total,
        "offset": offset,
        "from": _iso(date_from),
        "to": _iso(date_to),
        "items": [_appointment(a, slugs) for a in rows],
    }


def get_current_meds(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    patient = _patient(db, args.get("patient"), required=True)
    items = meds_svc.current_items(db, patient.id)
    names = _doctor_names(db, {i.prescriber_id for i in items})
    version = meds_svc.latest_version(db, patient.id)
    today = appointments_svc.today_local()
    ledd = ledd_substances.ledd_for_items(db, [meds_svc.item_snapshot(i, None) for i in items])
    return _compact(
        {
            "patient": patient.slug,
            "ledd": _ledd(ledd),
            "version": version.version_no if version else None,
            "saved_at": _iso(version.changed_at.date()) if version else None,
            "effective_date": _iso(version.effective_date) if version else None,
            "list_note": version.list_note if version else None,
            "items": [
                _compact(
                    {
                        "id": i.id,
                        "name": i.name,
                        "strength": i.strength,
                        "quantity": i.quantity,
                        "dose_note": i.dose_note,
                        "slot": i.slot,
                        "time": i.time.strftime("%H:%M") if i.time else None,
                        "days": list(i.days) if i.days else None,
                        "schedule": i.schedule_note,
                        "instructions": i.instructions,
                        "prescriber": names.get(i.prescriber_id),
                        "reason": i.reason,
                        "start_date": _iso(i.start_date),
                        "end_date": _iso(i.end_date),
                        "not_started_yet": True if i.start_date and i.start_date > today else None,
                        "source_entry_id": i.source_entry_id,
                    }
                )
                for i in items
            ],
            "avoid": [
                _compact(
                    {
                        "id": a.id,
                        "substance": a.substance,
                        "reason": a.reason,
                        "source_entry_id": a.source_entry_id,
                    }
                )
                for a in meds_svc.current_avoid(db, patient.id)
            ],
        }
    )


def _ledd(result: dict[str, Any]) -> dict[str, Any] | None:
    """The LEDD breakdown for the chat (ticket #133): a reference
    approximation, never a dose recommendation."""
    if not result["has_dopaminergic"]:
        return None
    return _compact(
        {
            "total_mg_per_day": result["total_mg"],
            "note": "Aproximación de referencia (Tomlinson 2010 / Jost 2023); no es una dosis.",
            "contributions": [
                _compact(
                    {
                        "substance": c["label"],
                        "medications": c["names"],
                        "daily_dose_mg": c["daily_dose_mg"],
                        "factor": c["factor"],
                        "levodopa_base_mg": c.get("base_mg"),
                        "ledd_mg": c["mg"],
                        "source": c["source"],
                    }
                )
                for c in result["contributions"]
            ],
            "as_needed_not_counted": [
                _compact({"medication": a["name"], "up_to_mg": a["max_mg"]})
                for a in result["as_needed"]
            ],
            "not_calculated": [
                {"medication": n["name"], "reason": n["reason_text"]}
                for n in result["not_calculated"]
            ],
            "pending_confirmation": [p["name"] for p in result["pending"]],
        }
    )


def get_med_history(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    patient = _patient(db, args.get("patient"), required=True)
    medication = _str(args.get("medication"), "medication")
    limit, offset = _page(args)
    events, _ = meds_svc.list_events(db, patient.id, limit=None)
    if medication:
        needle = _fold(medication)
        events = [e for e in events if needle in _fold(e["medication"] or "")]
    page = events[offset : offset + limit]
    versions = meds_svc.list_versions(db, patient.id)[:10]
    learned = ledd_substances.learned_map(db)
    version_items = meds_svc.version_items(db, patient.id)
    return {
        "patient": patient.slug,
        "events_total": len(events),
        "offset": offset,
        "events": [
            _compact(
                {
                    "id": e["id"],
                    "date": _iso(e["event_date"]),
                    "kind": e["kind"],
                    "medication": e["medication"],
                    "detail": _excerpt(e["detail"]),
                    "source_entry_id": e["source_entry_id"],
                    "pending_review": True if e["review_status"] != "ok" else None,
                }
            )
            for e in page
        ],
        "list_versions": [
            _compact(
                {
                    "version": v["version_no"],
                    "saved_at": _iso(v["changed_at"].date()) if v["changed_at"] else None,
                    "effective_date": _iso(v["effective_date"]),
                    "by": v["changed_by_name"],
                    "summary": _excerpt(v["change_summary"], 600),
                    "ledd_mg": ledd_substances.ledd_for_items(
                        db, version_items.get(v["version_no"], []), learned
                    )["total_mg"],
                }
            )
            for v in versions
        ],
    }


def _fold(text: str) -> str:
    return strip_accents(text).lower()


def _lab_conditions(db: DbSession, args: dict[str, Any]) -> list[ColumnElement[bool]]:
    patient = _patient(db, args.get("patient"))
    conds: list[ColumnElement[bool]] = []
    if patient:
        conds.append(LabReport.patient_id == patient.id)
    date_from = _date(args.get("date_from"), "date_from")
    date_to = _date(args.get("date_to"), "date_to")
    if date_from:
        conds.append(reports_svc.report_date >= date_from)
    if date_to:
        conds.append(reports_svc.report_date <= date_to)
    q = _str(args.get("q"), "q")
    if q:
        name = reports_svc._name_matches(db, q)
        if name is not None:
            conds.append(name)
    if args.get("out_of_range_only"):
        conds.append(LabResult.flag.in_(reports_svc.OUT_OF_RANGE_FLAGS))
    return conds


def _value(r: LabResult) -> str | None:
    return (
        r.value_text
        if r.value_text is not None
        else (str(r.value_num) if r.value_num is not None else None)
    )


def _ref(r: LabResult) -> str | None:
    if r.ref_text:
        return r.ref_text
    if r.ref_low is not None or r.ref_high is not None:
        low = "" if r.ref_low is None else str(r.ref_low)
        high = "" if r.ref_high is None else str(r.ref_high)
        return f"{low}–{high}"
    return None


def search_lab_results(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    conds = _lab_conditions(db, args)
    limit = _int(args.get("limit"), "limit", default=30, low=1, high=100)
    offset = _int(args.get("offset"), "offset", default=0, low=0)
    where = and_(*conds) if conds else None
    base = select(LabResult, LabReport).join(LabReport, LabReport.id == LabResult.report_id)
    count = (
        select(func.count())
        .select_from(LabResult)
        .join(LabReport, LabReport.id == LabResult.report_id)
    )
    if where is not None:
        base = base.where(where)
        count = count.where(where)
    total = db.scalar(count) or 0
    rows = db.execute(
        base.order_by(
            reports_svc.report_date.desc(), LabReport.id.desc(), LabResult.position, LabResult.id
        )
        .limit(limit)
        .offset(offset)
    ).all()
    slugs = _slugs(db)
    return {
        "total": total,
        "offset": offset,
        "items": [
            _compact(
                {
                    "report_id": report.id,
                    "date": _iso(reports_svc.effective_date(report)),
                    "patient": slugs.get(report.patient_id) if report.patient_id else None,
                    "lab": report.lab_name,
                    "panel": r.panel,
                    "test": r.raw_name,
                    "value": _value(r),
                    "comparator": r.comparator,
                    "unit": r.unit,
                    "ref": _ref(r),
                    "flag": r.flag,
                    "pending_review": True if report.status != "ok" else None,
                }
            )
            for r, report in rows
        ],
    }


def get_lab_report(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    report_id = _int(args.get("id"), "id")
    report = db.get(LabReport, report_id) if report_id else None
    if report is None:
        raise ToolInputError(f"No existe el informe {args.get('id')}.")
    results = sorted(report.results, key=lambda r: (r.position, r.id))
    return _compact(
        {
            "id": report.id,
            "date": _iso(reports_svc.effective_date(report)),
            "collected_on": _iso(report.collected_on),
            "reported_on": _iso(report.reported_on),
            "patient": _slugs(db).get(report.patient_id) if report.patient_id else None,
            "lab": report.lab_name,
            "status": report.status,
            "results": [
                _compact(
                    {
                        "panel": r.panel,
                        "test": r.raw_name,
                        "value": _value(r),
                        "comparator": r.comparator,
                        "unit": r.unit,
                        "ref": _ref(r),
                        "flag": r.flag,
                    }
                )
                for r in results
            ],
        }
    )


# ── File tools ───────────────────────────────────────────────────────────────

FILE_READY = (
    "Archivo creado. La familia lo ve como descarga debajo de tu respuesta; "
    "menciónalo por su nombre."
)
STORAGE_UNAVAILABLE = (
    "No se pueden guardar archivos en este momento (almacenamiento no disponible)."
)
STORE_FAILED = "No pude guardar el archivo. Puedes intentarlo de nuevo una vez."
RENDER_FAILED = "No pude generar el PDF. Simplifica el contenido o inténtalo de nuevo una vez."


def _spec(parse: Callable[[dict[str, Any]], Any], args: dict[str, Any]) -> Any:
    try:
        return parse(args)
    except files.FileSpecError as exc:
        raise ToolInputError(str(exc)) from None


def _store(ctx: ToolContext, data: bytes, file_type: Any, filename: str) -> dict[str, Any]:
    """Store ``data`` as an unlinked attachment (committed) and register it."""
    if ctx.storage is None:
        raise ToolInputError(STORAGE_UNAVAILABLE)
    try:
        storage = ctx.storage()
    except StorageNotConfigured:
        raise ToolInputError(STORAGE_UNAVAILABLE) from None
    try:
        row = attachments_svc.store_upload(
            ctx.db, storage, data=data, file_type=file_type, filename=filename, user_id=ctx.user_id
        )
        ctx.db.commit()
    except Exception as exc:
        ctx.db.rollback()
        logger.error("chat: could not store a generated file (%s)", type(exc).__name__)
        raise ToolInputError(STORE_FAILED) from None
    info = {"id": row.id, "filename": row.filename, "mime": row.mime, "size": row.size}
    ctx.attachment_ids.append(row.id)
    ctx.files.append(info)
    return {"file_id": row.id, "filename": row.filename, "size": row.size, "note": FILE_READY}


def generate_pdf(ctx: ToolContext, args: dict[str, Any]) -> Any:
    spec = _spec(files.parse_pdf, args)
    patient = _patient(ctx.db, spec.patient)
    # Display name only (Papá / Mamá): never the full name or the CIP.
    patient_name = patient.display_name if patient else None
    # Give the connection back: none is held while the child process renders.
    ctx.db.rollback()
    doc = ChatDocument(title=spec.title, sections=spec.sections, patient_name=patient_name)
    try:
        data = render_chat_document(doc)
    except pdf_svc.PdfRenderError:
        raise ToolInputError(RENDER_FAILED) from None
    today = appointments_svc.today_local()
    name = files.output_filename(spec.filename, spec.title, "pdf", today)
    return _store(ctx, data, attachments_svc.PDF, name)


def generate_xlsx(ctx: ToolContext, args: dict[str, Any]) -> Any:
    spec = _spec(files.parse_xlsx, args)
    data = files.build_xlsx(spec)
    today = appointments_svc.today_local()
    name = files.output_filename(spec.filename, spec.title, "xlsx", today)
    return _store(ctx, data, attachments_svc.XLSX, name)


# ── Ready-made documents (#34) ───────────────────────────────────────────────

CARD_NOTE = (
    "El PDF incluye los datos de identificación del paciente (nombre completo, cédula, "
    "fecha de nacimiento); tú no los conoces: no los menciones ni los inventes."
)


def _doctor(db: DbSession, value: Any) -> Doctor:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolInputError('Falta "doctor": usa el id de list_doctors o su nombre.')
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ToolInputError('"doctor" debe ser un id o un nombre.')
    text = str(value).strip()
    if text.isdigit():
        doctor = db.get(Doctor, int(text))
        if doctor is None:
            raise ToolInputError(f"No existe el médico {text}. Búscalo con list_doctors.")
        return doctor
    matches = match_doctors(db, text)
    if not matches:
        raise ToolInputError(f'No encuentro al médico "{text}". Búscalo con list_doctors.')
    if len(matches) > 1:
        ids = ", ".join(str(d.id) for d in matches)
        raise ToolInputError(f'"{text}" coincide con varios médicos (ids {ids}): usa el id.')
    return matches[0]


def previsit_summary(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    patient = _patient(db, args.get("patient"), required=True)
    doctor = _doctor(db, args.get("doctor"))
    summary = previsit_svc.previsit_summary(db, patient.id, doctor.id)
    db.rollback()  # no connection held while the child process renders
    try:
        data = summaries_pdf.render_previsit(summary)
    except pdf_svc.PdfRenderError:
        raise ToolInputError(RENDER_FAILED) from None
    stored = _store(ctx, data, attachments_svc.PDF, summaries_pdf.previsit_filename(summary))
    return _compact(
        {
            **stored,
            "patient": summary.patient_slug,
            "doctor_id": summary.doctor.id,
            "period_from": _iso(summary.period_from),
            "last_visit": _iso(summary.last_visit),
            "no_previous_visit": True if summary.last_visit is None else None,
            "counts": summary.counts(),
        }
    )


def emergency_card(ctx: ToolContext, args: dict[str, Any]) -> Any:
    db = ctx.db
    patient = _patient(db, args.get("patient"), required=True)
    card = card_svc.emergency_card(db, patient.id)
    db.rollback()
    try:
        data = summaries_pdf.render_emergency_card(card)
    except pdf_svc.PdfRenderError:
        raise ToolInputError(RENDER_FAILED) from None
    stored = _store(ctx, data, attachments_svc.PDF, summaries_pdf.emergency_filename(card))
    # Never the full name, CIP or birth date: counts only.
    return {
        **stored,
        "patient": card.patient_slug,
        "counts": card.counts(),
        "privacy": CARD_NOTE,
    }


# ── Proposals (#41) ──────────────────────────────────────────────────────────

PROPOSAL_KINDS = ("create_entry", "update_entry", "create_appointment", "med_change", "doctor")
PROPOSAL_NOTE = (
    "Propuesta creada en «Pendientes»: todavía NO está guardada. Un familiar debe revisarla "
    "y aprobarla allí. Dilo así en tu respuesta (la familia ve una tarjeta con el enlace)."
)
DUPLICATE_MESSAGES = {
    "entry": "Ya hay una entrada igual en la bitácora ese día: no creé la propuesta.",
    "appointment": "Ya hay una cita de ese paciente a esa hora (o ese día): no creé la propuesta.",
    "medication": "Ese medicamento ya está en la lista actual: no creé la propuesta.",
    "doctor": "Ese médico ya está registrado (nombre o alias): no creé la propuesta.",
    "pending": "Ya hay una propuesta igual esperando en «Pendientes»: no creé otra.",
}
# Appended to an entry's current text (``changes.add_*``), never rewritten.
_APPEND_FIELDS = {"add_indications": "indications", "add_family_notes": "family_notes"}
_NO_PATIENT_ID = 'No pongas patient_id en payload: indica el paciente con "patient".'


def _payload_entry(payload: dict[str, Any], patient: Patient | None) -> dict[str, Any]:
    if patient is None:
        raise ToolInputError('Falta "patient": usa "papa" o "mama".')
    if "patient_id" in payload:
        raise ToolInputError(_NO_PATIENT_ID)
    return {**payload, "patient_id": patient.id}


def _payload_update(db: DbSession, payload: dict[str, Any], patient: Patient | None):
    entry_id = payload.get("entry_id")
    valid_id = isinstance(entry_id, int) and not isinstance(entry_id, bool) and entry_id > 0
    entry = db.get(LogEntry, entry_id) if valid_id else None
    if entry is None:
        raise ToolInputError("No existe esa entrada: búscala con search_log_entries (entry_id).")
    if patient is not None and entry.patient_id != patient.id:
        raise ToolInputError("Esa entrada es del otro paciente.")
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ToolInputError('Falta "changes": los campos de la entrada que cambian.')
    changes = dict(changes)
    for blocked in ("patient_id", "attachment_ids", "review_status"):
        if blocked in changes:
            raise ToolInputError(f'"{blocked}" no se puede cambiar desde el chat.')
    # Append-only text (as in the WhatsApp analysis): the model sees only
    # excerpts of an entry, so a rewritten field could lose text on approval.
    for add, field_name in _APPEND_FIELDS.items():
        if field_name in changes:
            raise ToolInputError(
                f'"{field_name}" no se puede reescribir desde el chat: usa "{add}" con solo '
                "el texto nuevo (se añade al final del texto actual)."
            )
    adds = {add: changes.pop(add) for add in _APPEND_FIELDS if add in changes}
    for add, extra in adds.items():
        if not isinstance(extra, str) or not extra.strip():
            raise ToolInputError(f'"{add}" debe ser texto.')
    if adds:
        try:
            # Same helper as #39: appends after the full current text and
            # skips text the field already contains.
            changes.update(pipeline.entry_changes(entry, adds))
        except pipeline.InvalidProposal:
            if not changes:
                raise ToolInputError(
                    "La entrada ya contiene ese texto: no hay nada nuevo que proponer."
                ) from None
    return {**{k: v for k, v in payload.items() if k != "changes"}, "changes": changes}, entry


def _payload_appointment(payload: dict[str, Any], patient: Patient | None) -> dict[str, Any]:
    payload = _payload_entry(payload, patient)
    if "starts_at" in payload or "all_day" in payload:
        raise ToolInputError('Indica la fecha con "date" (AAAA-MM-DD) y la hora con "time".')
    day = _date(payload.pop("date", None), "date")
    if day is None:
        raise ToolInputError('Falta "date" (AAAA-MM-DD) de la cita.')
    at = _str(payload.pop("time", None), "time", 5)
    if at is not None and not re.fullmatch(r"\d{1,2}:\d{2}", at):
        raise ToolInputError('"time" debe ser HH:MM (24 h).')
    payload["starts_at"] = f"{day.isoformat()}T{(at or '00:00').zfill(5)}:00"
    payload["all_day"] = at is None
    return payload


def _check_doctors(db: DbSession, ids: list[Any]) -> None:
    for doctor_id in ids:
        if doctor_id is not None and (
            not isinstance(doctor_id, int) or db.get(Doctor, doctor_id) is None
        ):
            raise ToolInputError(f"No existe el médico {doctor_id}. Búscalo con list_doctors.")


def propose_change(ctx: ToolContext, args: dict[str, Any]) -> Any:
    """Create a ``pending`` suggestion (never a record): the family approves it
    in «Pendientes». Committed at once, like a generated file; the engine
    announces it (SSE ``suggestion``) and lists it on the final answer."""
    db = ctx.db
    kind = args.get("kind")
    if kind not in PROPOSAL_KINDS:
        raise ToolInputError(f'"kind" debe ser uno de: {", ".join(PROPOSAL_KINDS)}.')
    payload = args.get("payload")
    if not isinstance(payload, dict) or not payload:
        raise ToolInputError('"payload" debe ser un objeto con los datos propuestos.')
    rationale = _str(args.get("rationale"), "rationale", 2000)
    if not rationale:
        raise ToolInputError('Falta "rationale": qué dijo la familia, en una o dos frases.')
    patient = _patient(
        db,
        args.get("patient"),
        required=kind in ("create_entry", "create_appointment", "med_change"),
    )
    payload = dict(payload)
    patient_id: int | None = None
    if kind == "create_entry":
        payload = _payload_entry(payload, patient)
        patient_id = patient.id
    elif kind == "create_appointment":
        payload = _payload_appointment(payload, patient)
        patient_id = patient.id
    elif kind == "update_entry":
        payload, entry = _payload_update(db, payload, patient)
        patient_id = entry.patient_id
    elif kind == "med_change":
        patient_id = patient.id
        item_id = payload.get("item_id")
        if payload.get("action") in ("stop", "change") and (
            not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0
        ):
            raise ToolInputError(
                'Para "stop" o "change" falta "item_id": el id del medicamento en get_current_meds.'
            )
        version = meds_svc.latest_version(db, patient.id)
        payload.setdefault("base_version", version.version_no if version else 0)
    try:
        model = suggestions_svc.parse_payload(kind, payload)
    except suggestions_svc.SuggestionError as exc:
        raise ToolInputError(str(exc)) from None

    # What the approval would refuse anyway, said now so the model can fix it.
    if kind == "create_entry":
        next_appt = model.next_appointment
        _check_doctors(db, [model.doctor_id, next_appt.doctor_id if next_appt else None])
        if model.entry_date > appointments_svc.today_local():
            raise ToolInputError(
                "La bitácora registra hechos pasados: para algo futuro propón una cita "
                "(create_appointment)."
            )
    elif kind == "update_entry":
        changes = model.changes
        _check_doctors(db, [changes.doctor_id] if "doctor_id" in changes.model_fields_set else [])
        if changes.entry_date and changes.entry_date > appointments_svc.today_local():
            raise ToolInputError("La fecha de una entrada no puede ser futura.")
    elif kind == "create_appointment":
        _check_doctors(db, [model.doctor_id])
    elif kind == "med_change" and model.item is not None:
        _check_doctors(db, [model.item.prescriber_id])

    duplicate = pipeline.find_duplicate(db, kind, model, patient_id)
    if duplicate:
        raise ToolInputError(DUPLICATE_MESSAGES.get(duplicate, DUPLICATE_MESSAGES["pending"]))
    try:
        row = suggestions_svc.create_suggestion(
            db,
            kind=kind,
            payload=payload,
            rationale=rationale,
            source="chat",
            patient_id=patient_id,
            chat_session_id=ctx.session_id,
            created_by=ctx.user_id,
        )
        summary = suggestions_svc.short_summary(db, kind, row.payload)
        info = {"id": row.id, "kind": kind, "patient_id": row.patient_id, "summary": summary}
        slug = _slugs(db).get(row.patient_id) if row.patient_id else None
        db.commit()
    except suggestions_svc.SuggestionError as exc:
        db.rollback()
        raise ToolInputError(str(exc)) from None
    ctx.suggestions.append(info)
    return _compact(
        {
            "suggestion_id": info["id"],
            "kind": kind,
            "patient": slug,
            "summary": summary,
            "status": "pending",
            "note": PROPOSAL_NOTE,
        }
    )


# ── Registry ─────────────────────────────────────────────────────────────────

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="list_patients",
        description=(
            "Lista los pacientes (Papá y Mamá) con su id, identificador, diagnósticos y notas "
            "generales."
        ),
        input_schema=_schema({}),
        status="Consultando los pacientes…",
        handler=list_patients,
    ),
    Tool(
        name="list_doctors",
        description=(
            "Lista los médicos registrados (nombre, alias, especialidad, clínica, contacto). "
            "Filtra con q por nombre, alias o especialidad (sin importar tildes)."
        ),
        input_schema=_schema(
            {
                "q": {"type": "string", "description": "Texto a buscar."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Incluir médicos inactivos (por defecto no).",
                },
            }
        ),
        status="Consultando los médicos…",
        handler=list_doctors,
    ),
    Tool(
        name="search_log_entries",
        description=(
            "Busca en la bitácora (consultas, visitas, llamadas, exámenes, observaciones). "
            "q busca palabras (prefijos, sin importar tildes; todas deben aparecer) en el "
            "título, las indicaciones, las notas de la familia y el nombre del médico. Devuelve "
            "entradas resumidas, de la más reciente a la más antigua; usa get_log_entry para el "
            "texto completo."
        ),
        input_schema=_schema(
            {
                "patient": _PATIENT,
                "q": {"type": "string", "description": "Palabras a buscar."},
                "doctor_id": {"type": "integer", "description": "Id del médico."},
                "entry_type": {"type": "string", "enum": list(ENTRY_TYPES)},
                "date_from": _DATE,
                "date_to": _DATE,
                "order": {"type": "string", "enum": ["desc", "asc"]},
                "limit": _LIMIT,
                "offset": _OFFSET,
            }
        ),
        status="Buscando en la bitácora…",
        handler=search_log_entries,
    ),
    Tool(
        name="get_log_entry",
        description=(
            "Una entrada de la bitácora completa: indicaciones, notas de la familia, archivos "
            "adjuntos y la próxima cita que se anotó en ella."
        ),
        input_schema=_schema({"id": {"type": "integer"}}, ["id"]),
        status="Leyendo una entrada de la bitácora…",
        handler=get_log_entry,
    ),
    Tool(
        name="list_appointments",
        description=(
            "Lista citas médicas. Sin fechas devuelve las próximas (desde hoy). Estados: "
            "programada, realizada, cancelada."
        ),
        input_schema=_schema(
            {
                "patient": _PATIENT,
                "doctor_id": {"type": "integer"},
                "status": {"type": "string", "enum": list(APPOINTMENT_STATUSES)},
                "date_from": _DATE,
                "date_to": _DATE,
                "order": {"type": "string", "enum": ["asc", "desc"]},
                "limit": _LIMIT,
                "offset": _OFFSET,
            }
        ),
        status="Consultando las citas…",
        handler=list_appointments,
    ),
    Tool(
        name="get_current_meds",
        description=(
            "La lista actual de medicamentos de un paciente, por horario (como la hoja "
            "impresa), con la lista EVITAR y la fecha de la última versión guardada."
        ),
        input_schema=_schema({"patient": _PATIENT}, ["patient"]),
        status="Consultando los medicamentos…",
        handler=get_current_meds,
    ),
    Tool(
        name="get_med_history",
        description=(
            "Historial de medicamentos de un paciente: eventos de inicio, suspensión, cambio de "
            "dosis o sustitución sacados de la bitácora (con la entrada de origen), y las "
            "últimas versiones guardadas de la lista con su resumen de cambios. Filtra con "
            "medication."
        ),
        input_schema=_schema(
            {
                "patient": _PATIENT,
                "medication": {"type": "string", "description": "Nombre o parte del nombre."},
                "limit": _LIMIT,
                "offset": _OFFSET,
            },
            ["patient"],
        ),
        status="Revisando el historial de medicamentos…",
        handler=get_med_history,
    ),
    Tool(
        name="search_lab_results",
        description=(
            "Busca resultados de laboratorio (una fila por prueba) del más reciente al más "
            "antiguo. q filtra por nombre de la prueba (también por su nombre canónico o alias, "
            "sin importar tildes). Útil para ver la evolución de un valor."
        ),
        input_schema=_schema(
            {
                "patient": _PATIENT,
                "q": {"type": "string", "description": "Nombre de la prueba, p. ej. glucosa."},
                "date_from": _DATE,
                "date_to": _DATE,
                "out_of_range_only": {
                    "type": "boolean",
                    "description": "Solo resultados marcados alto, bajo o anormal.",
                },
                "limit": {"type": "integer", "description": "1–100, por defecto 30."},
                "offset": _OFFSET,
            }
        ),
        status="Buscando resultados de laboratorio…",
        handler=search_lab_results,
    ),
    Tool(
        name="get_lab_report",
        description="Un informe de laboratorio completo con todos sus resultados.",
        input_schema=_schema({"id": {"type": "integer"}}, ["id"]),
        status="Leyendo un informe de laboratorio…",
        handler=get_lab_report,
    ),
    Tool(
        name="generate_pdf",
        description=(
            "Genera un PDF descargable con el formato de impresión de la app: título, "
            "paciente opcional y secciones con encabezado, texto en Markdown y/o una tabla. "
            "Úsalo solo cuando la familia pida un PDF, un documento o algo para imprimir, "
            "después de reunir los datos con las otras herramientas. Límites: "
            f"{files.MAX_SECTIONS} secciones, {files.MAX_MARKDOWN_CHARS} caracteres de "
            f"Markdown por sección, tablas de {files.MAX_PDF_ROWS} filas × "
            f"{files.MAX_PDF_COLUMNS} columnas. Devuelve el nombre del archivo creado."
        ),
        input_schema=_schema(
            {
                "title": {"type": "string", "description": "Título del documento."},
                "patient": _PATIENT,
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "markdown": {
                                "type": "string",
                                "description": "Texto en Markdown (listas, tablas, negritas).",
                            },
                            "table": {
                                "type": "object",
                                "properties": {
                                    "columns": {"type": "array", "items": {"type": "string"}},
                                    "rows": {
                                        "type": "array",
                                        "items": {"type": "array", "items": _CELL},
                                    },
                                },
                                "required": ["columns", "rows"],
                                "additionalProperties": False,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "filename": _FILENAME,
            },
            ["title", "sections"],
        ),
        status="Generando el PDF…",
        handler=generate_pdf,
    ),
    Tool(
        name="generate_xlsx",
        description=(
            "Genera un Excel (.xlsx) descargable: una hoja por elemento de sheets, con fila "
            "de encabezados y filas de valores (texto, números o booleanos; fechas como "
            "AAAA-MM-DD). Úsalo solo cuando la familia pida un Excel o una hoja de cálculo, "
            "después de reunir los datos con las otras herramientas. Límites: "
            f"{files.MAX_SHEETS} hojas, {files.MAX_XLSX_COLUMNS} columnas por hoja, "
            f"{files.MAX_CELLS} celdas en total. Devuelve el nombre del archivo creado."
        ),
        input_schema=_schema(
            {
                "title": {"type": "string", "description": "Título del libro."},
                "sheets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Nombre de la hoja (máximo 31 caracteres).",
                            },
                            "columns": {"type": "array", "items": {"type": "string"}},
                            "rows": {"type": "array", "items": {"type": "array", "items": _CELL}},
                        },
                        "required": ["name", "columns", "rows"],
                        "additionalProperties": False,
                    },
                },
                "filename": _FILENAME,
            },
            ["title", "sheets"],
        ),
        status="Generando el Excel…",
        handler=generate_xlsx,
    ),
    Tool(
        name="previsit_summary",
        description=(
            "Genera el PDF «Resumen pre-consulta» de un paciente para su próxima consulta con "
            "un médico: entradas de la bitácora desde la última consulta con ese médico (o de "
            "los últimos 12 meses si no hay ninguna), cambios de medicamentos en ese período, "
            "medicamentos actuales y EVITAR, laboratorios del período con los valores fuera "
            "de rango, próximas citas y un espacio en blanco para «Preguntas para el doctor». "
            "Úsalo cuando la familia lo pida; no hace falta reunir los datos antes. Devuelve "
            "el nombre del archivo y conteos."
        ),
        input_schema=_schema(
            {
                "patient": _PATIENT,
                "doctor": {
                    "type": ["integer", "string"],
                    "description": "El médico: su id (de list_doctors) o su nombre.",
                },
            },
            ["patient", "doctor"],
        ),
        status="Preparando el resumen pre-consulta…",
        handler=previsit_summary,
    ),
    Tool(
        name="emergency_card",
        description=(
            "Genera el PDF de una página «Tarjeta de emergencia» de un paciente: datos de "
            "identificación, diagnósticos, medicamentos actuales, lista EVITAR y médicos con "
            "sus teléfonos. Úsalo cuando la familia lo pida. Devuelve el nombre del archivo "
            "y conteos (nunca los datos de identificación)."
        ),
        input_schema=_schema({"patient": _PATIENT}, ["patient"]),
        status="Preparando la tarjeta de emergencia…",
        handler=emergency_card,
    ),
    Tool(
        name="propose_change",
        description=(
            "Propone un cambio en los datos de la app. NO lo guarda: crea una propuesta en "
            "«Pendientes» que un familiar revisa, edita si hace falta y aprueba. Úsalo cuando "
            "la familia te cuente información nueva para registrar (una consulta, una "
            "indicación, una cita, un cambio de medicamento, un médico nuevo), una propuesta "
            "por cada cambio. Antes busca con las otras herramientas los ids que hagan falta y "
            "que no esté ya registrado. payload según kind (fechas AAAA-MM-DD, horas HH:MM):\n"
            "- create_entry (patient obligatorio): entry_date (no futura), entry_type ("
            + ", ".join(ENTRY_TYPES)
            + "), title, y opcionales entry_time, doctor_id (de list_doctors), source_label "
            "(quién o dónde, si el médico no está registrado), channel (presencial, domicilio, "
            "whatsapp, telefono, correo), indications (lo que indicó el médico, Markdown), "
            "family_notes (observaciones de la familia), next_appointment {date, time, "
            "doctor_id, location, reason}. Hace falta doctor_id, source_label o title.\n"
            "- update_entry: entry_id (de search_log_entries) y changes con solo los campos "
            "que cambian (los de create_entry salvo indications y family_notes, que no se "
            "reescriben: para añadir texto usa changes.add_indications / "
            "changes.add_family_notes con solo el texto nuevo).\n"
            "- create_appointment (patient obligatorio): date, y opcionales time (sin hora = "
            "todo el día), doctor_id, location, reason, notes, duration_min.\n"
            "- med_change (patient obligatorio): action add | stop | change; medication (el "
            "nombre; para stop/change, exacto como en get_current_meds); item_id (de "
            "get_current_meds) para stop/change; item para add (name y slot obligatorios) o "
            "change (solo los campos que cambian): name, slot (al_despertar, desayuno, manana, "
            "almuerzo, tarde, cena, noche, madrugada, segun_necesidad, otros), strength, "
            "quantity, dose_note, time, days (1 = lunes … 7 = domingo), schedule_note, "
            "instructions, prescriber_id, reason, start_date.\n"
            "- doctor: name, y opcionales aliases, specialty, phone, email, clinic, address, "
            "notes.\n"
            "Si los datos no son válidos o ya existen, devuelve un error que explica qué "
            "corregir."
        ),
        input_schema=_schema(
            {
                "kind": {"type": "string", "enum": list(PROPOSAL_KINDS)},
                "patient": _PATIENT,
                "payload": {
                    "type": "object",
                    "description": "Los datos propuestos, según kind (ver la descripción).",
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "Qué dijo la familia y por qué lo propones, en una o dos frases "
                        "(se muestra en Pendientes)."
                    ),
                },
            },
            ["kind", "payload", "rationale"],
        ),
        status="Preparando una propuesta para Pendientes…",
        handler=propose_change,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def definitions() -> list[dict[str, Any]]:
    """The ``tools`` array of every request (static order and text)."""
    return [tool.definition() for tool in TOOLS]


def status_for(name: str) -> str:
    tool = TOOLS_BY_NAME.get(name)
    return tool.status if tool else "Consultando los datos…"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def cap_result(value: Any, max_chars: int = MAX_RESULT_CHARS) -> str:
    """JSON text of a handler result, at most ``max_chars`` long.

    A list (or a dict's ``items`` / ``events`` / ``results`` list) loses
    elements from the end and gains a ``truncated`` note; anything else is cut.
    """
    text = _dumps(value)
    if len(text) <= max_chars:
        return text
    if isinstance(value, list):
        value = {"items": value}
    if isinstance(value, dict):
        key = next(
            (k for k in ("items", "events", "results") if isinstance(value.get(k), list)), None
        )
        if key is not None:
            data = dict(value)
            items = list(data[key])
            data["truncated"] = TRUNCATED_NOTE
            while items:
                items.pop()
                data[key] = items
                data["shown"] = len(items)
                text = _dumps(data)
                if len(text) <= max_chars:
                    return text
    suffix = f"… [{TRUNCATED_NOTE}]"
    return text[: max(0, max_chars - len(suffix))] + suffix


@dataclass
class ToolRun:
    block: dict[str, Any]  # the tool_result content block
    ok: bool


def run_tool(ctx: ToolContext, tool_use_id: str, name: str, args: Any) -> ToolRun:
    """Run one tool call into a ``tool_result`` block (errors become ``is_error``)."""

    def error(message: str) -> ToolRun:
        return ToolRun(
            block={
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": message,
                "is_error": True,
            },
            ok=False,
        )

    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return error(f"Herramienta desconocida: {name}.")
    if not isinstance(args, dict):
        return error("Los argumentos deben ser un objeto JSON.")
    unknown = set(args) - set(tool.input_schema.get("properties", {}))
    if unknown:
        return error(f"Argumentos desconocidos: {', '.join(sorted(unknown))}.")
    try:
        result = tool.handler(ctx, args)
    except ToolInputError as exc:
        return error(str(exc))
    return ToolRun(
        block={"type": "tool_result", "tool_use_id": tool_use_id, "content": cap_result(result)},
        ok=True,
    )
