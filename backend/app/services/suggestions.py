"""Suggestions: create (for the daily analysis and the chat), list, approve,
reject (DESIGN §5.6, §6.7 step 5).

Rules:

- :func:`create_suggestion` is the one way to add a suggestion (#39 daily
  analysis, #41 chat). The payload is validated with the target API's schema
  (``app.schemas.suggestions.PAYLOAD_SCHEMAS``) and stored normalized;
  invalid input raises :class:`SuggestionError` (status 422). Nothing is
  ever applied without a person (DESIGN §11: no auto-apply in v1).
  Producer guards: a ``create_entry``/``update_entry`` payload may not carry
  ``attachment_ids`` (it would replace an entry's files; approval attaches
  the source media itself), and a ``med_change`` stop/change must target an
  item of that patient whose name matches ``medication``
  (:func:`medication_matches`: EVITAR normalizer, strength suffix ignored).
  The name is checked again at preview/approve (422 if it no longer matches).
- An approval with a ``payload`` is ``edited`` only when it differs from the
  proposal after both go through the kind's schema with defaults and nulls
  dropped (an untouched form submit stays ``approved``).
- :func:`approve` locks the row (``SELECT … FOR UPDATE``), refuses a decided
  one (409), applies the change through the same service functions the
  normal API calls — so validation and audit rows are identical and
  attributed to the approving user — and records ``status`` (``approved``,
  or ``edited`` when the person changed the payload), ``decided_by/at`` and
  ``result_ref``, all in one transaction. When the change does not pass
  validation it raises :class:`SuggestionError` with the service's Spanish
  message; the caller rolls back and the suggestion stays ``pending``.
- New bitácora entries get ``origin`` = the suggestion's source
  (``whatsapp``/``chat``) and ``origin_ref`` = ``{suggestion_id,
  message_ids}``. The source messages' files (audio, images, PDFs; not
  videos) are attached to the created/updated entry or the new appointment
  as **new rows pointing at the same stored object**
  (``attachments.copy_attachments``): an attachment has one owner, and
  re-linking would take the file away from its WhatsApp message.
  Medication changes and doctors have no attachments.
- ``med_change`` is applied as a full-list save (``meds.save_list``) built
  from the **current** list plus the one proposed change, with
  ``expected_version`` = the current version (or the version the person
  previewed, so a list saved in between answers 409). An item that is no
  longer on the list (stopped or removed since the proposal) → 422. The
  list's «Desde» and note are kept (``save_list``'s ``KEEP`` default; the
  payload carries neither).
- ``doctor``: creates the doctor (``doctors.create_doctor``; 409 naming the
  existing one when the name or an alias is taken), or, with ``doctor_id``,
  links the suggestion to that existing doctor without writing anything.
- ``lab_report``: reserved, no apply path (422); it can be rejected.
- Chat suggestions (#41, ``app.services.chat.tools.propose_change``):
  ``source="chat"``, ``chat_session_id`` and ``created_by`` (the family
  member who asked; also the audit row's user), no source messages.
  :func:`short_summary` is the one-line text of the chat's card.
- Doctor inference (#125, ``app.services.doctor_inference``):
  ``source="ai"``, an ``update_entry`` whose ``changes`` are **only**
  ``doctor_id`` (an active doctor) for an entry with no doctor, the reason
  in ``rationale``, no source messages. :func:`create_suggestion` refuses
  any other shape for that source, and :func:`approve` refuses (409
  ``ENTRY_HAS_DOCTOR``) once the entry has a doctor: the proposal only
  fills a missing one, never overwrites.
- Explicit requests (#134): a WhatsApp suggestion citing a «Bot: …» message
  has a rationale starting with :data:`EXPLICIT_PREFIX` (set by the
  analysis pipeline, :func:`explicit_rationale`; no column of its own).
  :func:`is_explicit` reads it back (``explicit_request`` in the API) and
  the pending list shows those first (``list_suggestions(explicit_first=…)``).
"""

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.models.attachment import Attachment
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.models.medication import MedItem
from app.models.patient import Patient
from app.models.suggestion import (
    SOURCE_AI,
    SOURCES,
    STATUS_APPROVED,
    STATUS_EDITED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Suggestion,
)
from app.models.whatsapp import WhatsappMessage
from app.schemas.appointments import AppointmentCreate
from app.schemas.log_entries import LogEntryCreate
from app.schemas.meds import MedItemIn
from app.schemas.people import DoctorCreate
from app.schemas.suggestions import PAYLOAD_SCHEMAS, MedChangePayload, UpdateEntryPayload
from app.services import appointments, audit, doctors, log_entries, meds, meds_checks
from app.services.attachments import AttachmentLinkError, copy_attachments

ENTITY = "suggestion"
# Rationale prefix of a suggestion that answers a «Bot: …» message (#134).
EXPLICIT_LABEL = "Pedido explícito"
EXPLICIT_PREFIX = EXPLICIT_LABEL + ": "
_EXPLICIT_LEAD = re.compile(r"^\s*pedido\s+expl[ií]cito\s*[:.,;\-–—]?\s*", re.IGNORECASE)
# Message files worth keeping with a record (the bitácora does not show videos).
ATTACHABLE_MEDIA = ("audio", "image", "pdf")

NOT_FOUND = "Sugerencia no encontrada."
ALREADY_DECIDED = "Esta sugerencia ya se decidió."
ATTACHMENT_CONFLICT = "Algún archivo adjunto no existe o pertenece a otro registro."
ENTRY_GONE = "La entrada que esta sugerencia quería cambiar ya no existe."
LAB_REPORT_NO_APPLY = (
    "Los informes de laboratorio se registran solos en Laboratorios; "
    "esta sugerencia solo se puede rechazar."
)
NO_PATIENT = "La sugerencia no indica el paciente."
MED_NOT_ON_LIST = "«{name}» ya no está en la lista actual de medicamentos."
MED_NO_CHANGE = "La lista de medicamentos ya refleja este cambio."
MED_LIST_CHANGED = (
    "La lista de medicamentos cambió desde la vista previa. Revisa el cambio de nuevo."
)
NOT_MED_CHANGE = "Solo los cambios de medicamentos tienen vista previa."
MED_MISMATCH = "La sugerencia no coincide con el medicamento actual."
NO_ATTACHMENT_IDS = (
    "Una sugerencia no puede indicar archivos: los de los mensajes de origen "
    "se adjuntan al aprobarla."
)
DOCTOR_LINK_WRONG_KIND = "Solo una sugerencia de médico se puede vincular a un médico existente."
ENTRY_HAS_DOCTOR = (
    "Esta entrada ya tiene médico; la sugerencia solo completaba uno que faltaba. Recházala."
)
AI_DOCTOR_ONLY = "Una sugerencia de la IA solo puede completar el médico de una entrada."
DOCTOR_NOT_ACTIVE = "El médico propuesto no existe o está inactivo."


class SuggestionError(ValueError):
    """A suggestion that cannot be created or decided; the message is Spanish
    and safe to show, ``status`` the HTTP status the API answers."""

    def __init__(self, message: str, status: int = 422, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    return f"Los datos propuestos no son válidos ({where}: {first.get('msg')})."


def parse_payload(kind: str, payload: dict[str, Any]) -> BaseModel:
    """The payload as the target API's schema; raises ``SuggestionError``."""
    schema = PAYLOAD_SCHEMAS.get(kind)
    if schema is None:
        raise SuggestionError("Tipo de sugerencia no válido.")
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise SuggestionError(_validation_message(exc)) from None


def normalize_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validated payload in its stored form (JSON types, unset fields left out)."""
    return parse_payload(kind, payload).model_dump(mode="json", exclude_unset=True)


def medication_key(name: str | None) -> str:
    """Comparison key for a medication name: the EVITAR normalizer (accents,
    case, punctuation), then the words before the first one with a digit, so
    a strength suffix does not matter («Prolopa 250 mg» → «prolopa»)."""
    key = meds_checks.normalize(name)
    return meds_checks._leading_words(key) or key


def medication_matches(medication: str, item_name: str | None) -> bool:
    """Whether the name a ``med_change`` shows is the targeted item's."""
    key = medication_key(medication)
    return bool(key) and key == medication_key(item_name)


def _comparable(kind: str, model: BaseModel) -> Any:
    """The payload for the "was it edited?" check: defaults and nulls dropped,
    an appointment's start on the Panamá clock (the form sends an offset, a
    producer may not)."""
    data = model.model_dump(mode="json", exclude_defaults=True)
    if kind == "create_appointment":
        data["starts_at"] = appointments.normalize_starts_at(
            model.starts_at,  # type: ignore[attr-defined]
            bool(model.all_day),  # type: ignore[attr-defined]
        ).isoformat()

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if v is not None}
        return value

    return strip(data)


def _check_producer_payload(db: DbSession, kind: str, model: BaseModel, patient_id) -> None:
    """Extra rules for AI producers (#39/#41), on top of the schema."""
    if kind == "create_entry" and "attachment_ids" in model.model_fields_set:
        raise SuggestionError(NO_ATTACHMENT_IDS)
    if kind == "update_entry" and "attachment_ids" in model.changes.model_fields_set:  # type: ignore[attr-defined]
        raise SuggestionError(NO_ATTACHMENT_IDS)
    if kind == "med_change" and model.action != "add":  # type: ignore[attr-defined]
        item = db.get(MedItem, model.item_id)  # type: ignore[attr-defined]
        if item is None or item.patient_id != patient_id:
            raise SuggestionError(MED_NOT_ON_LIST.format(name=model.medication))  # type: ignore[attr-defined]
        if not medication_matches(model.medication, item.name):  # type: ignore[attr-defined]
            raise SuggestionError(MED_MISMATCH)


def is_doctor_fill(row: Suggestion) -> bool:
    """A doctor inference proposal (#125): fills a missing ``doctor_id``."""
    changes = (row.payload or {}).get("changes") or {}
    return row.source == SOURCE_AI and row.kind == "update_entry" and set(changes) == {"doctor_id"}


def _check_doctor_fill(db: DbSession, kind: str, model: BaseModel) -> None:
    """``source="ai"``: only a doctor for an entry that has none (#125)."""
    if kind != "update_entry" or model.changes.model_fields_set != {"doctor_id"}:  # type: ignore[attr-defined]
        raise SuggestionError(AI_DOCTOR_ONLY)
    doctor = db.get(Doctor, model.changes.doctor_id)  # type: ignore[attr-defined]
    if doctor is None or not doctor.is_active:
        raise SuggestionError(DOCTOR_NOT_ACTIVE)
    entry = db.get(LogEntry, model.entry_id)  # type: ignore[attr-defined]
    if entry is not None and entry.doctor_id is not None:
        raise SuggestionError(ENTRY_HAS_DOCTOR, status=409)


def _patient_for(db: DbSession, kind: str, model: BaseModel, patient_id: int | None) -> int | None:
    if kind in ("create_entry", "create_appointment"):
        return model.patient_id  # type: ignore[attr-defined]
    if kind == "update_entry":
        entry = db.get(LogEntry, model.entry_id)  # type: ignore[attr-defined]
        if entry is None:
            raise SuggestionError(ENTRY_GONE)
        return entry.patient_id
    if kind == "med_change" and patient_id is None:
        raise SuggestionError(NO_PATIENT)
    return patient_id


def create_suggestion(
    db: DbSession,
    *,
    kind: str,
    payload: dict[str, Any],
    rationale: str | None,
    source_message_ids: list[int] | tuple[int, ...] = (),
    source: str = "whatsapp",
    patient_id: int | None = None,
    import_id: int | None = None,
    analysis_run_id: int | None = None,
    chat_session_id: int | None = None,
    created_by: int | None = None,
) -> Suggestion:
    """Validate and add a ``pending`` suggestion (audited; the user is
    ``created_by``, none for the WhatsApp analysis). The caller commits.

    ``patient_id`` is taken from the payload for new entries and appointments
    and from the entry for ``update_entry``; ``med_change`` needs it.
    ``lab_report`` is refused (no apply path in v1). A chat suggestion (#41)
    passes ``source="chat"``, its ``chat_session_id`` and the family member
    whose message led to it as ``created_by``. Raises
    :class:`SuggestionError`.
    """
    if source not in SOURCES:
        raise SuggestionError("Origen de la sugerencia no válido.")
    if chat_session_id is not None and source != "chat":
        raise SuggestionError("Origen de la sugerencia no válido.")
    if kind == "lab_report":
        raise SuggestionError(LAB_REPORT_NO_APPLY)
    model = parse_payload(kind, payload)
    patient_id = _patient_for(db, kind, model, patient_id)
    if patient_id is not None and db.get(Patient, patient_id) is None:
        raise SuggestionError("El paciente no existe.")
    _check_producer_payload(db, kind, model, patient_id)
    if source == SOURCE_AI:
        _check_doctor_fill(db, kind, model)
    ids = list(dict.fromkeys(int(i) for i in source_message_ids))
    if ids:
        found = set(db.scalars(select(WhatsappMessage.id).where(WhatsappMessage.id.in_(ids))))
        if len(found) != len(ids):
            raise SuggestionError("Algún mensaje de origen no existe.")
    row = Suggestion(
        kind=kind,
        source=source,
        patient_id=patient_id,
        payload=model.model_dump(mode="json", exclude_unset=True),
        rationale=(rationale or "").strip() or None,
        source_message_ids=ids,
        import_id=import_id,
        analysis_run_id=analysis_run_id,
        chat_session_id=chat_session_id,
        created_by=created_by,
        status=STATUS_PENDING,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        user_id=created_by,
        action=audit.ACTION_CREATE,
        entity=ENTITY,
        entity_id=row.id,
        after=audit.snapshot(row),
    )
    return row


# ── Listing ──────────────────────────────────────────────────────────────────


def filter_conditions(
    *, statuses: list[str] | None = None, patient_ids: list[int] | None = None
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if statuses:
        conds.append(Suggestion.status.in_(statuses))
    if patient_ids:
        # A new doctor belongs to no patient: it shows with either.
        conds.append(or_(Suggestion.patient_id.in_(patient_ids), Suggestion.patient_id.is_(None)))
    return conds


def explicit_rationale(rationale: str | None, explicit: bool) -> str:
    """The rationale to store for an analysis proposal: with
    :data:`EXPLICIT_PREFIX` in front when it answers a «Bot: …» message, and
    never starting with it otherwise (whatever lead-in the model wrote)."""
    rest = _EXPLICIT_LEAD.sub("", (rationale or "").strip(), count=1)
    if not explicit:
        return rest
    return EXPLICIT_PREFIX + rest if rest else EXPLICIT_LABEL + "."


def is_explicit(row: Suggestion) -> bool:
    """Whether a suggestion answers a «Bot: …» message (#134)."""
    return row.source == "whatsapp" and (row.rationale or "").startswith(EXPLICIT_LABEL)


def _explicit_order() -> ColumnElement[int]:
    explicit = and_(
        Suggestion.source == "whatsapp", Suggestion.rationale.startswith(EXPLICIT_LABEL)
    )
    return case((explicit, 0), else_=1)


def list_suggestions(
    db: DbSession,
    conds: list[ColumnElement[bool]],
    *,
    limit: int = 50,
    offset: int = 0,
    explicit_first: bool = False,
) -> tuple[list[Suggestion], int]:
    """Newest first; ``explicit_first``: answers to «Bot: …» messages
    (:func:`is_explicit`) before the rest (the pending inbox)."""
    where = and_(*conds) if conds else None
    stmt = select(Suggestion)
    count = select(func.count()).select_from(Suggestion)
    if where is not None:
        stmt, count = stmt.where(where), count.where(where)
    total = db.scalar(count) or 0
    order = [Suggestion.created_at.desc(), Suggestion.id.desc()]
    if explicit_first:
        order.insert(0, _explicit_order())
    rows = db.scalars(stmt.order_by(*order).limit(limit).offset(offset))
    return list(rows), total


def count_pending(db: DbSession, patient_ids: list[int] | None = None) -> int:
    conds = filter_conditions(statuses=[STATUS_PENDING], patient_ids=patient_ids)
    return db.scalar(select(func.count()).select_from(Suggestion).where(and_(*conds))) or 0


def source_messages(
    db: DbSession, message_ids: list[int]
) -> dict[int, tuple[WhatsappMessage, Attachment | None]]:
    """Messages by id, each with its file's attachment row (or None)."""
    if not message_ids:
        return {}
    rows = db.execute(
        select(WhatsappMessage, Attachment)
        .outerjoin(Attachment, Attachment.id == WhatsappMessage.attachment_id)
        .where(WhatsappMessage.id.in_(message_ids))
    )
    return {message.id: (message, attachment) for message, attachment in rows}


def _entry_context(entry: LogEntry) -> dict[str, Any]:
    data = audit.snapshot(entry)
    data.pop("origin_ref", None)
    data["doctor_name"] = entry.doctor.name if entry.doctor else None
    return data


def _med_item_context(item: MedItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "strength": item.strength,
        "quantity": item.quantity,
        "dose_note": item.dose_note,
        "slot": item.slot,
        "time": item.time.strftime("%H:%M") if item.time else None,
        "days": item.days,
        "instructions": item.instructions,
    }


def context_for(db: DbSession, row: Suggestion) -> dict[str, Any]:
    """What the card needs besides the payload (see ``SuggestionOut``)."""
    payload = row.payload or {}
    if row.kind == "update_entry":
        entry = db.get(LogEntry, payload.get("entry_id"))
        return {"entry": _entry_context(entry) if entry else None}
    if row.kind == "med_change" and row.patient_id is not None:
        latest = meds.latest_version(db, row.patient_id)
        version_no = latest.version_no if latest else 0
        base = payload.get("base_version")
        item = None
        if payload.get("item_id"):
            current = {i.id: i for i in meds.current_items(db, row.patient_id)}
            found = current.get(payload["item_id"])
            item = _med_item_context(found) if found else None
        return {
            "version_no": version_no,
            "stale": base is not None and base != version_no,
            "item": item,
        }
    if row.kind == "doctor":
        name = payload.get("name") or ""
        seen: dict[int, Doctor] = {}
        for text in [name, *(payload.get("aliases") or [])]:
            for doctor in doctors.match_doctors(db, text):
                seen.setdefault(doctor.id, doctor)
        return {
            "duplicates": [
                {"id": d.id, "name": d.name, "specialty": d.specialty} for d in seen.values()
            ]
        }
    return {}


MED_ACTION_LABELS = {"add": "Agregar", "stop": "Suspender", "change": "Cambiar"}
SUMMARY_CHARS = 120


def _day(value: Any) -> str | None:
    """'2026-10-09…' → '09/10/2026' (None when it is not a date)."""
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return None


def short_summary(db: DbSession, kind: str, payload: dict[str, Any]) -> str:
    """One Spanish line describing a proposal, for the chat's card (#41):
    «09/10/2026 · Control de cardiología», «Agregar Losartán»…"""

    def join(*parts: Any) -> str:
        text = " · ".join(str(p).strip() for p in parts if p and str(p).strip())
        return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1].rstrip() + "…"

    if kind == "create_entry":
        title = payload.get("title") or payload.get("source_label")
        return join(_day(payload.get("entry_date")), title)
    if kind == "update_entry":
        entry = db.get(LogEntry, payload.get("entry_id"))
        if entry is None:
            return ""
        return join(entry.entry_date.strftime("%d/%m/%Y"), entry.title or entry.source_label)
    if kind == "create_appointment":
        starts = str(payload.get("starts_at") or "")
        at = None if payload.get("all_day") or len(starts) < 16 else starts[11:16]
        return join(_day(starts), at, payload.get("reason") or payload.get("location"))
    if kind == "med_change":
        action = MED_ACTION_LABELS.get(payload.get("action"), "")
        return join(f"{action} {payload.get('medication') or ''}")
    if kind == "doctor":
        return join(payload.get("name"))
    return ""


# ── Deciding ─────────────────────────────────────────────────────────────────


def get_for_decision(db: DbSession, suggestion_id: int) -> Suggestion:
    row = db.get(Suggestion, suggestion_id, with_for_update=True)
    if row is None:
        raise SuggestionError(NOT_FOUND, status=404)
    if row.status != STATUS_PENDING:
        raise SuggestionError(ALREADY_DECIDED, status=409)
    return row


def _source_files(db: DbSession, row: Suggestion) -> list[Attachment]:
    if not row.source_message_ids:
        return []
    return list(
        db.scalars(
            select(Attachment)
            .join(WhatsappMessage, WhatsappMessage.attachment_id == Attachment.id)
            .where(
                WhatsappMessage.id.in_(row.source_message_ids),
                WhatsappMessage.media_kind.in_(ATTACHABLE_MEDIA),
            )
            .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
        )
    )


def _next_appointment(value) -> dict[str, Any] | None:
    return None if value is None else value.model_dump(exclude_unset=True)


def _apply_create_entry(db, row, data: LogEntryCreate, user_id) -> dict[str, Any]:
    entry = log_entries.create_entry(
        db,
        data.model_dump(exclude={"attachment_ids", "next_appointment"}),
        user_id=user_id,
        origin=row.source,
        origin_ref={"suggestion_id": row.id, "message_ids": list(row.source_message_ids)},
        attachment_ids=data.attachment_ids,
        next_appointment=_next_appointment(data.next_appointment),
    )
    copy_attachments(
        db, _source_files(db, row), owner_type="log_entry", owner_id=entry.id, user_id=user_id
    )
    return {"entity": "log_entry", "id": entry.id}


def _apply_update_entry(db, row, data: UpdateEntryPayload, user_id) -> dict[str, Any]:
    entry = db.get(LogEntry, data.entry_id, with_for_update=True)
    if entry is None:
        raise SuggestionError(ENTRY_GONE)
    if is_doctor_fill(row) and entry.doctor_id is not None:
        raise SuggestionError(ENTRY_HAS_DOCTOR, status=409)
    body = data.changes
    extra = {"attachment_ids", "next_appointment"}
    changes = body.model_dump(include=body.model_fields_set - extra)
    next_appt = (
        _next_appointment(body.next_appointment)
        if "next_appointment" in body.model_fields_set
        else log_entries.UNCHANGED
    )
    log_entries.update_entry(
        db,
        entry,
        changes,
        user_id=user_id,
        attachment_ids=body.attachment_ids,
        next_appointment=next_appt,
    )
    copy_attachments(
        db, _source_files(db, row), owner_type="log_entry", owner_id=entry.id, user_id=user_id
    )
    return {"entity": "log_entry", "id": entry.id}


def _apply_create_appointment(db, row, data: AppointmentCreate, user_id) -> dict[str, Any]:
    appt = appointments.create_appointment(db, data.model_dump(), user_id=user_id)
    copy_attachments(
        db, _source_files(db, row), owner_type="appointment", owner_id=appt.id, user_id=user_id
    )
    return {"entity": "appointment", "id": appt.id}


def med_change_lists(
    db: DbSession, patient_id: int, change: MedChangePayload
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """The full-list save body (items, avoid) for ``change`` applied to the
    patient's current list, and the current version number."""
    meds.get_patient(db, patient_id)
    latest = meds.latest_version(db, patient_id)
    items: list[dict[str, Any]] = [
        {"id": i.id, **{f: getattr(i, f) for f in meds.ITEM_FIELDS}}
        for i in meds.current_items(db, patient_id)
    ]
    index = next((n for n, i in enumerate(items) if i["id"] == change.item_id), None)
    if change.action != "add" and index is None:
        raise SuggestionError(MED_NOT_ON_LIST.format(name=change.medication))
    if change.action != "add" and not medication_matches(change.medication, items[index]["name"]):
        raise SuggestionError(MED_MISMATCH)
    proposed = change.item.model_dump(exclude_unset=True) if change.item else {}
    if change.action == "add":
        items.append(proposed)
    elif change.action == "stop":
        items.pop(index)
    else:
        items[index] = {**items[index], **proposed}
    try:
        items = [MedItemIn.model_validate(i).model_dump() for i in items]
    except ValidationError as exc:
        raise SuggestionError(_validation_message(exc)) from None
    avoid = [
        {
            "id": r.id,
            "substance": r.substance,
            "reason": r.reason,
            "source_entry_id": r.source_entry_id,
        }
        for r in meds.current_avoid(db, patient_id)
    ]
    return items, avoid, latest.version_no if latest else 0


def preview_med_change(
    db: DbSession, row: Suggestion, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Dry run of a ``med_change`` (nothing is written; the transaction is
    rolled back). ``payload`` previews an edited proposal."""
    if row.kind != "med_change":
        raise SuggestionError(NOT_MED_CHANGE, status=400)
    if row.status != STATUS_PENDING:
        raise SuggestionError(ALREADY_DECIDED, status=409)
    if row.patient_id is None:
        raise SuggestionError(NO_PATIENT)
    change = parse_payload("med_change", payload if payload is not None else row.payload)
    patient_id, base = row.patient_id, change.base_version  # read before any rollback
    try:
        items, avoid, version_no = med_change_lists(db, patient_id, change)
    except meds.MedsError as exc:
        raise SuggestionError(str(exc), status=exc.status) from None
    warnings = meds_checks.avoid_warnings(items, avoid)
    try:
        outcome = meds.save_list(
            db,
            patient_id,
            items=items,
            avoid=avoid,
            expected_version=version_no,
            change_summary=change.change_summary,
            dry_run=True,
            user_id=None,
        )
    except meds.MedsError as exc:
        raise SuggestionError(str(exc), status=exc.status) from None
    return {
        "version_no": version_no,
        "auto_summary": outcome.auto_summary,
        "diff": outcome.diff,
        "warnings": warnings,
        "stale": base is not None and base != version_no,
    }


def _apply_med_change(
    db, row, data: MedChangePayload, user_id, *, expected_version, change_summary
) -> dict[str, Any]:
    if row.patient_id is None:
        raise SuggestionError(NO_PATIENT)
    patient_id = row.patient_id
    items, avoid, version_no = med_change_lists(db, patient_id, data)
    if expected_version is not None and expected_version != version_no:
        raise SuggestionError(MED_LIST_CHANGED, status=409)
    try:
        outcome = meds.save_list(
            db,
            patient_id,
            items=items,
            avoid=avoid,
            expected_version=version_no,
            change_summary=change_summary or data.change_summary,
            user_id=user_id,
        )
    except meds.MedsError as exc:
        raise SuggestionError(
            MED_LIST_CHANGED if exc.status == 409 else str(exc), status=exc.status
        ) from None
    if not outcome.saved:
        # save_list rolled the transaction back (and released our row lock).
        raise SuggestionError(MED_NO_CHANGE)
    version = outcome.version
    return {
        "entity": "med_list_version",
        "id": version.id,
        "patient_id": patient_id,
        "version_no": version.version_no,
    }


def _apply_doctor(db, row, data: DoctorCreate, user_id, *, doctor_id) -> dict[str, Any]:
    if doctor_id is not None:
        doctor = db.get(Doctor, doctor_id)
        if doctor is None:
            raise SuggestionError("El médico no existe.", status=404)
        return {"entity": "doctor", "id": doctor.id, "linked": True}
    try:
        doctor = doctors.create_doctor(db, data.model_dump(), user_id=user_id)
    except doctors.DoctorConflict as exc:
        raise SuggestionError(
            f"{exc} Usa «Vincular» para asociar la sugerencia a ese médico.",
            status=409,
            doctor={"id": exc.existing.id, "name": exc.existing.name},
        ) from None
    return {"entity": "doctor", "id": doctor.id}


def approve(
    db: DbSession,
    suggestion_id: int,
    *,
    user_id: int,
    payload: dict[str, Any] | None = None,
    expected_version: int | None = None,
    change_summary: str | None = None,
    doctor_id: int | None = None,
) -> Suggestion:
    """Apply a pending suggestion and mark it decided (module docstring has the
    rules). The caller commits, or rolls back on :class:`SuggestionError`."""
    row = get_for_decision(db, suggestion_id)
    if doctor_id is not None and row.kind != "doctor":
        raise SuggestionError(DOCTOR_LINK_WRONG_KIND, status=400)
    if row.kind == "lab_report":
        raise SuggestionError(LAB_REPORT_NO_APPLY)
    data = parse_payload(row.kind, payload if payload is not None else row.payload)
    applied = data.model_dump(mode="json", exclude_unset=True)
    edited = payload is not None and _comparable(row.kind, data) != _comparable(
        row.kind, parse_payload(row.kind, row.payload)
    )
    before = audit.snapshot(row)
    try:
        if row.kind == "create_entry":
            ref = _apply_create_entry(db, row, data, user_id)
        elif row.kind == "update_entry":
            ref = _apply_update_entry(db, row, data, user_id)
        elif row.kind == "create_appointment":
            ref = _apply_create_appointment(db, row, data, user_id)
        elif row.kind == "med_change":
            ref = _apply_med_change(
                db,
                row,
                data,
                user_id,
                expected_version=expected_version,
                change_summary=change_summary,
            )
        else:
            ref = _apply_doctor(db, row, data, user_id, doctor_id=doctor_id)
    except (log_entries.LogEntryError, appointments.AppointmentError) as exc:
        raise SuggestionError(str(exc)) from None
    except AttachmentLinkError:
        raise SuggestionError(ATTACHMENT_CONFLICT) from None
    except meds.MedsError as exc:
        raise SuggestionError(str(exc), status=exc.status) from None
    row.status = STATUS_EDITED if edited else STATUS_APPROVED
    row.decided_by = user_id
    row.decided_at = datetime.now(UTC)
    row.result_ref = ref
    db.flush()
    after = audit.snapshot(row)
    if edited:
        after["applied_payload"] = applied
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_APPROVE,
        entity=ENTITY,
        entity_id=row.id,
        before=before,
        after=after,
    )
    return row


def reject(
    db: DbSession, suggestion_id: int, *, user_id: int, reason: str | None = None
) -> Suggestion:
    """Mark a pending suggestion rejected (audited); nothing else is written.
    The caller commits."""
    row = get_for_decision(db, suggestion_id)
    before = audit.snapshot(row)
    row.status = STATUS_REJECTED
    row.decided_by = user_id
    row.decided_at = datetime.now(UTC)
    row.decision_note = reason
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_REJECT,
        entity=ENTITY,
        entity_id=row.id,
        before=before,
        after=audit.snapshot(row),
    )
    return row
