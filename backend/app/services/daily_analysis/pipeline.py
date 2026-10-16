"""Messages → suggestions (DESIGN §6.7 steps 4 and 6). Used by the
``analyze_import`` job (``jobs.py``, a ``daily`` import) and the
``analyze_backfill`` job (``backfill.py``, an :class:`AnalysisRun`): the same
steps with another prompt and a few backfill rules (:class:`Options`). The
*target* is the import or the run: it holds the stats, names the advisory
lock and is recorded on the suggestions (``import_id`` /
``analysis_run_id``).

1. **Messages**: for an import, those it stored first
   (``whatsapp_messages.import_id``); for a run, every stored message of its
   date range (:func:`range_messages`, whatever import stored it). In time
   order.
2. **Readiness** (:func:`waiting_media`): an audio/image/PDF message is
   *resolved* once it has a transcript / classification, or a recorded
   failure (``audio_transcripts`` row, ``classification.status = failed``).
   An unresolved one is *waited for* only while a ``transcribe_audio`` /
   ``classify_media`` job for its file content is still queued or running; a
   job that failed without recording anything (missing key) is not waited
   for.
3. **Grouping** by Panamá day (:func:`group_by_day`). Days already in
   ``stats.analysis.days_done`` are skipped (a retried or continued job never
   analyzes a day twice); at most ``Options.max_days`` days per job.
4. Per day: a cheap pre-filter (:func:`is_relevant`: some text with letters,
   a transcript, or an image/PDF classified as medical) skips days with
   nothing to read without a call. The rest are rendered as text
   (:func:`render_message`) and sent in chunks of at most
   ``CHUNK_MAX_CHARS`` characters, one Claude call per chunk, with the
   record context (``context.build_context``, built once per job).
5. Each proposal is turned into a payload (:func:`to_payload`; invalid →
   ``dropped_invalid``), checked against existing records and pending
   suggestions (:func:`find_duplicate`; → ``skipped_duplicate``) and added
   with ``suggestions.create_suggestion`` (refused → ``dropped_invalid``).
   Each day commits with its counts, so a crash loses at most one day.
6. **Backfill rules** (``Options.backfill``, :func:`out_of_scope`): only the
   focus patient (and new doctors); a cross-check never proposes
   ``update_entry`` / ``med_change``; no appointment before today; no
   medication change from a day up to the current list's «Desde» (the list
   already reflects it) → ``skipped_out_of_scope``. Rejected suggestions
   count as duplicates too (a re-run never re-proposes what was rejected),
   and a cross-check also matches an entry with the same doctor up to
   ``CROSS_CHECK_DAYS`` days away (the bitácora may date a visit differently
   from the chat).
7. **Explicit requests** (#134): a message whose text starts with the word
   «Bot» (:func:`is_bot_request`: case/accent-insensitive, after leading
   spaces, quotes or WhatsApp formatting marks; «Bot:», «Bot corrección…»;
   never «Bote» or «robot») is the family addressing the app. Its rendered
   line carries ``[PEDIDO EXPLÍCITO]`` (the v2 prompts say what the tag
   means) and every suggestion citing one gets the rationale prefix
   ``suggestions.EXPLICIT_PREFIX`` (listed first in «Pendientes», with a
   badge). Validation, scope and duplicate rules still apply; a tagged
   proposal they skip, and a request no created suggestion cites, are
   counted (``explicit_*`` stats) and logged with ids and counts only.

Nothing here logs message text, senders, transcripts, payloads or rationales:
ids and counts only.
"""

import difflib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.analysis_run import AnalysisRun
from app.models.appointment import Appointment
from app.models.attachment import Attachment
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.log_entry import LogEntry
from app.models.suggestion import STATUS_PENDING, STATUS_REJECTED, Suggestion
from app.models.transcription import AudioTranscript
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.prompts import PromptSpec
from app.services import doctors as doctor_service
from app.services import meds, suggestions
from app.services.appointments import local_datetime, normalize_starts_at, to_local
from app.services.bitacora_import.extract import Usage
from app.services.daily_analysis import analyze
from app.services.daily_analysis.context import AnalysisContext, build_context
from app.services.suggestions import SuggestionError, medication_key

logger = logging.getLogger("app.daily_analysis")

# Job kinds whose results a message waits for (#36, #37).
TRANSCRIBE_KIND = "transcribe_audio"
CLASSIFY_KIND = "classify_media"
WAITED_MEDIA = ("audio", "image", "pdf")
CHUNK_MAX_CHARS = 40_000
MESSAGE_MAX_CHARS = 4_000
TITLE_SIMILARITY = 0.6
NO_DOCTOR_SIMILARITY = 0.85
APPOINTMENT_WINDOW = timedelta(hours=1)
CROSS_CHECK_DAYS = 2
MEDICAL_CATEGORIES = frozenset({"lab_report", "prescription", "medical_document"})
CATEGORY_LABELS = {
    "lab_report": "informe de laboratorio",
    "prescription": "receta",
    "medical_document": "documento médico",
    "other": "sin contenido médico",
}
HINT_LABELS = {"papa": "Papá", "mama": "Mamá"}
MISSING_NOTE = "(Algún archivo de ese día no se pudo procesar automáticamente.)"
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_LETTERS = re.compile(r"[^\W\d_]{2,}")
# «Bot» as the first word of the accent-stripped, case-folded text, after any
# spaces, quotes, WhatsApp *_~ marks, «@», «>» or dashes: «Bot:», «BOT
# corrección», «"Bot, …"»; not «Bote», «Bots», «bot2» or «robot».
_BOT_REQUEST = re.compile(r"^[\s\"'«»“”‘’*_~@>\-–—]*bot(?!\w)")
EXPLICIT_TAG = "[PEDIDO EXPLÍCITO]"


@dataclass(frozen=True)
class Options:
    spec: PromptSpec
    model: str
    max_days: int
    # Backfill (#40); the defaults are the daily analysis.
    backfill: bool = False
    patient_id: int | None = None  # focus: other patients' proposals are dropped
    cross_check: bool = False  # only events missing from the bitácora
    preamble: str = ""  # the run's instructions, above the record context
    today: date | None = None  # context "as of" (meds, appointments); no appointment before
    meds_since: date | None = None  # no med change from a day up to this one


Target = WhatsappImport | AnalysisRun


class InvalidProposal(ValueError):
    """A proposal that cannot become a suggestion (counted, never logged)."""


class AnalysisBusy(Exception):
    """Another job is analyzing the same import right now."""


class AnalysisFailed(Exception):
    """A call failed in a way the job must report (config) or retry."""

    def __init__(self, code: str, *, retryable: bool, config: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.config = config


# ── Messages, readiness, grouping ────────────────────────────────────────────


def new_messages(db: DbSession, import_id: int) -> list[WhatsappMessage]:
    return list(
        db.scalars(
            select(WhatsappMessage)
            .where(WhatsappMessage.import_id == import_id)
            .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
        )
    )


def range_messages(db: DbSession, date_from: date, date_to: date) -> list[WhatsappMessage]:
    """Every stored message sent on the Panamá days ``date_from``…``date_to``
    (inclusive), whatever import stored it."""
    return list(
        db.scalars(
            select(WhatsappMessage)
            .where(
                WhatsappMessage.sent_at >= local_datetime(date_from),
                WhatsappMessage.sent_at < local_datetime(date_to + timedelta(days=1)),
            )
            .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
        )
    )


def _media(messages: list[WhatsappMessage]) -> list[WhatsappMessage]:
    return [m for m in messages if m.attachment_id is not None and m.media_kind in WAITED_MEDIA]


def unresolved_media(db: DbSession, messages: list[WhatsappMessage]) -> list[WhatsappMessage]:
    """Audio/image/PDF messages without a result or a recorded failure."""
    media = _media(messages)
    if not media:
        return []
    shas = dict(
        db.execute(
            select(Attachment.id, Attachment.sha256).where(
                Attachment.id.in_({m.attachment_id for m in media})
            )
        ).all()
    )
    failed_audio = set(
        db.scalars(
            select(AudioTranscript.sha256).where(
                AudioTranscript.sha256.in_(set(shas.values())), AudioTranscript.status == "failed"
            )
        )
    )
    out = []
    for m in media:
        if m.media_kind == "audio":
            if m.transcript is None and shas.get(m.attachment_id) not in failed_audio:
                out.append(m)
        elif m.classification is None:
            out.append(m)
    return out


def waiting_media(db: DbSession, messages: list[WhatsappMessage]) -> list[WhatsappMessage]:
    """Unresolved media whose transcription/classification job is still queued
    or running (worth waiting for)."""
    unresolved = unresolved_media(db, messages)
    if not unresolved:
        return []
    shas = dict(
        db.execute(
            select(Attachment.id, Attachment.sha256).where(
                Attachment.id.in_({m.attachment_id for m in unresolved})
            )
        ).all()
    )
    job_attachment_ids = {
        int(v)
        for v in db.scalars(
            select(Job.payload["attachment_id"].as_integer()).where(
                Job.kind.in_((TRANSCRIBE_KIND, CLASSIFY_KIND)),
                Job.status.in_(("queued", "running")),
            )
        )
        if v is not None
    }
    if not job_attachment_ids:
        return []
    busy_shas = set(
        db.scalars(select(Attachment.sha256).where(Attachment.id.in_(job_attachment_ids)))
    )
    return [m for m in unresolved if shas.get(m.attachment_id) in busy_shas]


def local_day(value: datetime) -> date:
    return to_local(value).date()


def group_by_day(messages: list[WhatsappMessage]) -> dict[date, list[WhatsappMessage]]:
    """Messages by Panamá calendar day, each day in time order."""
    days: dict[date, list[WhatsappMessage]] = defaultdict(list)
    for m in sorted(messages, key=lambda m: (m.sent_at, m.id)):
        days[local_day(m.sent_at)].append(m)
    return dict(sorted(days.items()))


# ── Rendering ────────────────────────────────────────────────────────────────


def _clip(text: str, limit: int = MESSAGE_MAX_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _media_text(m: WhatsappMessage) -> str | None:
    kind = m.media_kind
    if kind is None:
        return None
    if m.attachment_id is None:
        return "(archivo no incluido en la exportación)"
    if kind == "audio":
        if m.transcript is not None and m.transcript.strip():
            return f"Nota de voz, transcripción automática: «{_clip(m.transcript)}»"
        if m.transcript is not None:
            return "(nota de voz sin palabras)"
        return "(nota de voz sin transcripción)"
    if kind in ("image", "pdf"):
        label = "foto" if kind == "image" else "PDF"
        c = m.classification or {}
        if c.get("status") == "done":
            parts = [c.get("description") or label]
            category = CATEGORY_LABELS.get(c.get("category") or "")
            if category:
                parts.append(f"tipo: {category}")
            hint = HINT_LABELS.get(c.get("patient_hint") or "")
            if hint:
                parts.append(f"paciente probable: {hint}")
            return f"({label}: " + "; ".join(parts) + ")"
        return f"({label} sin descripción)"
    return "(video)" if kind == "video" else "(archivo)"


def is_bot_request(text: str | None) -> bool:
    """Whether a message text is an explicit request to the app: its first
    word is «Bot» (module docstring, step 7)."""
    if not text:
        return False
    head = doctor_service.strip_accents(text[:100]).casefold()
    return _BOT_REQUEST.match(head) is not None


def render_message(m: WhatsappMessage) -> str:
    head = f"[#{m.id} {to_local(m.sent_at).strftime('%H:%M')}] {m.sender or 'Desconocido'}"
    if m.forwarded:
        head += " (reenviado)"
    if is_bot_request(m.text):
        head += " " + EXPLICIT_TAG
    parts = [p for p in (_clip(m.text or ""), _media_text(m)) if p]
    return head + ": " + (" ".join(parts) if parts else "(vacío)")


def is_relevant(messages: list[WhatsappMessage]) -> bool:
    """Whether a day has anything worth a call: text with letters, a
    transcript with words, or an image/PDF classified as medical."""
    for m in messages:
        if _LETTERS.search(m.text or "") or _LETTERS.search(m.transcript or ""):
            return True
        c = m.classification or {}
        if c.get("status") == "done" and c.get("category") in MEDICAL_CATEGORIES:
            return True
    return False


def chunk_lines(lines: list[tuple[int, str]], max_chars: int = CHUNK_MAX_CHARS):
    """Split ``(message_id, line)`` pairs into chunks of at most ``max_chars``
    characters (a single longer line is its own chunk)."""
    chunks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for item in lines:
        n = len(item[1]) + 1
        if current and size + n > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += n
    if current:
        chunks.append(current)
    return chunks


def day_header(day: date, part: int, parts: int, missing: int, *, backfill: bool = False) -> str:
    label = "MENSAJES" if backfill else "MENSAJES NUEVOS"
    text = f"{label} del {WEEKDAYS[day.weekday()]} {day.isoformat()} (hora de Panamá)"
    if parts > 1:
        text += f", parte {part} de {parts}"
    if missing:
        text += (
            f"\nNota: {missing} archivo(s) de este día no se pudieron transcribir o "
            "describir a tiempo; no adivines su contenido."
        )
    return text


# ── Proposals → payloads ─────────────────────────────────────────────────────


def _s(value: Any) -> str | None:
    """A schema string ("" = none) as a value or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _id(value: Any) -> int | None:
    """A schema id (0 = none) as a value or None."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _doctor(value: Any, ctx: AnalysisContext) -> int | None:
    doctor_id = _id(value)
    if doctor_id is not None and doctor_id not in ctx.doctor_ids:
        raise InvalidProposal("unknown doctor")
    return doctor_id


def _patient(proposal: dict[str, Any], ctx: AnalysisContext) -> int:
    patient_id = ctx.patients.get(proposal.get("patient"))
    if patient_id is None:
        raise InvalidProposal("unknown patient")
    return patient_id


def _time(value: Any) -> str | None:
    text = _s(value)
    if text is None:
        return None
    try:
        return time.fromisoformat(text).strftime("%H:%M")
    except ValueError:
        raise InvalidProposal("bad time") from None


def entry_changes(entry: LogEntry, proposal: dict[str, Any]) -> dict[str, Any]:
    """An ``update_entry`` proposal as PATCH fields, **append-only**: the model
    sends only the text to add (``add_indications`` / ``add_family_notes``),
    appended here after the entry's full current text (the model only sees a
    short summary, so it could never rewrite a field safely); ``title`` only
    fills an entry without one. Text already contained in the field adds
    nothing. Raises :class:`InvalidProposal` when nothing changes."""
    changes: dict[str, Any] = {}
    title = _s(proposal.get("title"))
    if title and not _s(entry.title):
        changes["title"] = title
    for key, field_name in (
        ("add_indications", "indications"),
        ("add_family_notes", "family_notes"),
    ):
        extra = _s(proposal.get(key))
        if not extra:
            continue
        current = (getattr(entry, field_name) or "").rstrip()
        if current and _norm_text(extra) in _norm_text(current):
            continue
        changes[field_name] = f"{current}\n\n{extra}" if current else extra
    if not changes:
        raise InvalidProposal("no changes")
    return changes


def _norm_text(value: str) -> str:
    return " ".join(doctor_service.normalize_name(value).split())


def to_payload(
    db: DbSession, proposal: Any, ctx: AnalysisContext
) -> tuple[str, dict[str, Any], int | None]:
    """``(kind, payload, patient_id)`` for ``create_suggestion``; raises
    :class:`InvalidProposal`. Shapes follow ``app.schemas.suggestions``."""
    if not isinstance(proposal, dict):
        raise InvalidProposal("not an object")
    kind = proposal.get("kind")
    payload: dict[str, Any] = {}
    if kind == "create_entry":
        patient_id = _patient(proposal, ctx)
        payload = {"patient_id": patient_id, "entry_date": _s(proposal.get("entry_date"))}
        payload["entry_type"] = proposal.get("entry_type")
        _put(payload, "entry_time", _time(proposal.get("entry_time")))
        _put(payload, "doctor_id", _doctor(proposal.get("doctor_id"), ctx))
        for key in ("source_label", "channel", "title", "indications", "family_notes"):
            _put(payload, key, _s(proposal.get(key)))
        return kind, payload, patient_id
    if kind == "update_entry":
        patient_id = _patient(proposal, ctx)
        entry_id = _id(proposal.get("entry_id"))
        entry = db.get(LogEntry, entry_id) if entry_id else None
        if entry is None or entry.patient_id != patient_id:
            raise InvalidProposal("unknown entry")
        return kind, {"entry_id": entry_id, "changes": entry_changes(entry, proposal)}, patient_id
    if kind == "create_appointment":
        patient_id = _patient(proposal, ctx)
        day = _s(proposal.get("date"))
        if day is None:
            raise InvalidProposal("no date")
        at = _time(proposal.get("time"))
        payload = {
            "patient_id": patient_id,
            "starts_at": f"{day}T{at or '00:00'}:00",
            "all_day": at is None,
        }
        _put(payload, "doctor_id", _doctor(proposal.get("doctor_id"), ctx))
        for key in ("location", "reason", "notes"):
            _put(payload, key, _s(proposal.get(key)))
        return kind, payload, patient_id
    if kind == "med_change":
        patient_id = _patient(proposal, ctx)
        action = proposal.get("action")
        payload = {
            "action": action,
            "medication": _s(proposal.get("medication")),
            "base_version": ctx.med_versions.get(patient_id, 0),
        }
        if action in ("stop", "change"):
            item_id = _id(proposal.get("item_id"))
            item = ctx.med_items.get(item_id) if item_id else None
            if item is None or item.patient_id != patient_id:
                raise InvalidProposal("not a current item")
            payload["item_id"] = item_id
        if action in ("add", "change"):
            item_fields: dict[str, Any] = {}
            for key in (
                "name",
                "strength",
                "quantity",
                "dose_note",
                "slot",
                "schedule_note",
                "instructions",
                "reason",
            ):
                _put(item_fields, key, _s(proposal.get(key)))
            _put(item_fields, "prescriber_id", _doctor(proposal.get("prescriber_id"), ctx))
            if action == "change":
                item_fields.pop("name", None)  # the item keeps its name
            payload["item"] = item_fields
        return kind, payload, patient_id
    if kind == "doctor":
        payload = {"name": _s(proposal.get("name"))}
        aliases = proposal.get("aliases")
        if isinstance(aliases, list):
            payload["aliases"] = [a.strip() for a in aliases if isinstance(a, str) and a.strip()]
        for key in ("specialty", "clinic", "phone"):
            _put(payload, key, _s(proposal.get(key)))
        return kind, payload, None
    raise InvalidProposal("unknown kind")


# ── Duplicates ───────────────────────────────────────────────────────────────


def same_entry(
    doctor_id: int | None,
    type_a: str | None,
    title_a: str | None,
    type_b: str | None,
    title_b: str | None,
) -> bool:
    """Whether two entries of one patient on one date with the same doctor
    (``doctor_id``, possibly None) are the same event. A near-duplicate that
    is let through only costs a rejection in «Pendientes»; a new event
    wrongly skipped is lost, so:

    - the entry type must match;
    - with a doctor: normalized titles alike (either empty, one containing
      the other, or ``difflib`` ratio ≥ ``TITLE_SIMILARITY``);
    - without a doctor: both titles present and, after dropping their common
      leading words («Observación: caída» / «Observación: fiebre» →
      «caida» / «fiebre»), identical or ratio ≥ ``NO_DOCTOR_SIMILARITY``.
    """
    if (type_a or "") != (type_b or ""):
        return False
    ka, kb = _norm_text(title_a or ""), _norm_text(title_b or "")
    if doctor_id is not None:
        if not ka or not kb or ka in kb or kb in ka:
            return True
        return difflib.SequenceMatcher(None, ka, kb).ratio() >= TITLE_SIMILARITY
    if not ka or not kb:
        return False
    wa, wb = ka.split(), kb.split()
    while wa and wb and wa[0] == wb[0]:
        wa, wb = wa[1:], wb[1:]
    ra, rb = " ".join(wa), " ".join(wb)
    if ra == rb:
        return True
    if not ra or not rb:
        return False
    return difflib.SequenceMatcher(None, ra, rb).ratio() >= NO_DOCTOR_SIMILARITY


def _pending(
    db: DbSession, kind: str, patient_id: int | None, *, include_rejected: bool = False
) -> list[Suggestion]:
    statuses = (STATUS_PENDING, STATUS_REJECTED) if include_rejected else (STATUS_PENDING,)
    stmt = select(Suggestion).where(Suggestion.kind == kind, Suggestion.status.in_(statuses))
    if patient_id is None:
        stmt = stmt.where(Suggestion.patient_id.is_(None))
    else:
        stmt = stmt.where(Suggestion.patient_id == patient_id)
    return list(db.scalars(stmt))


def _starts_at(model) -> datetime:
    return normalize_starts_at(model.starts_at, bool(model.all_day))


def find_duplicate(
    db: DbSession,
    kind: str,
    model,
    patient_id: int | None,
    *,
    include_rejected: bool = False,
    entry_window_days: int = 0,
) -> str | None:
    """Why the validated proposal ``model`` duplicates an existing record or
    pending suggestion (a short code), or None. ``include_rejected``: rejected
    suggestions count too (backfill). ``entry_window_days``: an entry with the
    same (known) doctor up to that many days away also counts (cross-check)."""

    def pending(kind: str, patient_id: int | None) -> list[Suggestion]:
        return _pending(db, kind, patient_id, include_rejected=include_rejected)

    if kind == "create_entry":
        window = timedelta(days=max(0, entry_window_days))
        entries = db.scalars(
            select(LogEntry).where(
                LogEntry.patient_id == patient_id,
                LogEntry.entry_date.between(model.entry_date - window, model.entry_date + window),
            )
        )
        title = model.title or model.source_label
        for e in entries:
            if e.entry_date != model.entry_date and model.doctor_id is None:
                continue
            if e.doctor_id == model.doctor_id and same_entry(
                model.doctor_id, e.entry_type, e.title or e.source_label, model.entry_type, title
            ):
                return "entry"
        for s in pending(kind, patient_id):
            p = s.payload
            if (
                p.get("entry_date") == model.entry_date.isoformat()
                and p.get("doctor_id") == model.doctor_id
                and same_entry(
                    model.doctor_id,
                    p.get("entry_type"),
                    p.get("title") or p.get("source_label"),
                    model.entry_type,
                    title,
                )
            ):
                return "pending"
        return None
    if kind == "create_appointment":
        start = _starts_at(model)
        rows = db.scalars(
            select(Appointment).where(
                Appointment.patient_id == patient_id,
                Appointment.status != "cancelada",
                Appointment.starts_at.between(start - timedelta(days=1), start + timedelta(days=1)),
            )
        )
        for a in rows:
            if model.doctor_id is not None and a.doctor_id != model.doctor_id:
                continue
            if model.all_day or a.all_day:
                if to_local(a.starts_at).date() == start.date():
                    return "appointment"
            elif abs(a.starts_at - start) <= APPOINTMENT_WINDOW:
                return "appointment"
        for s in pending(kind, patient_id):
            p = s.payload
            try:
                other = normalize_starts_at(
                    datetime.fromisoformat(p["starts_at"]), bool(p.get("all_day"))
                )
            except (KeyError, TypeError, ValueError):
                continue
            if other.date() == start.date() and p.get("doctor_id") == model.doctor_id:
                return "pending"
        return None
    if kind == "update_entry":
        for s in pending(kind, patient_id):
            if s.payload.get("entry_id") == model.entry_id:
                return "pending"
        return None
    if kind == "med_change":
        key = medication_key(model.medication)
        if model.action == "add":
            names = [medication_key(i.name) for i in meds.current_items(db, patient_id)]
            if key and key in names:
                return "medication"
        for s in pending(kind, patient_id):
            p = s.payload
            if (
                p.get("action") == model.action
                and p.get("item_id") == model.item_id
                and medication_key(p.get("medication")) == key
            ):
                return "pending"
        return None
    if kind == "doctor":
        keys = {doctor_service.normalize_name(model.name)}
        keys.update(doctor_service.normalize_name(a) for a in model.aliases)
        keys.discard("")
        for d in db.scalars(select(Doctor)):
            if doctor_service.doctor_keys(d) & keys:
                return "doctor"
        for s in pending(kind, None):
            if doctor_service.normalize_name(s.payload.get("name")) in keys:
                return "pending"
        return None
    return None


# ── Running ──────────────────────────────────────────────────────────────────


def empty_stats(options: Options) -> dict[str, Any]:
    return {
        "prompt_version": options.spec.version,
        "model": options.model,
        "status": "pending",
        "waits": 0,
        "days": 0,
        "days_skipped": 0,
        "days_failed": 0,
        "days_done": [],
        "suggestions_created": 0,
        "dropped_invalid": 0,
        "skipped_duplicate": 0,
        "skipped_out_of_scope": 0,
        # «Bot: …» messages (step 7): seen in analyzed days, suggestions created
        # for them, tagged proposals skipped (invalid / scope / duplicate), and
        # requests no created suggestion cites.
        "explicit_requests": 0,
        "explicit_created": 0,
        "explicit_skipped": 0,
        "explicit_unanswered": 0,
        "missing_media": 0,
        "usage": Usage().as_dict(),
        "cost_usd": 0.0,
    }


def get_stats(target: Target, options: Options) -> dict[str, Any]:
    """The analysis stats: ``stats.analysis`` of an import, ``stats`` of a run."""
    if isinstance(target, AnalysisRun):
        current = target.stats
    else:
        current = (target.stats or {}).get("analysis")
    stats = empty_stats(options)
    if isinstance(current, dict):
        stats.update(current)
    return stats


def save_stats(target: Target, stats: dict[str, Any]) -> None:
    # JSONB without mutation tracking: assign a new dict.
    if isinstance(target, AnalysisRun):
        target.stats = dict(stats)
    else:
        target.stats = {**(target.stats or {}), "analysis": dict(stats)}


def _add_usage(stats: dict[str, Any], usage: Usage, model: str) -> None:
    totals = dict(stats["usage"])
    for key, value in usage.as_dict().items():
        totals[key] = totals.get(key, 0) + value
    stats["usage"] = totals
    cost = usage.cost_usd(model)
    if cost is not None:
        stats["cost_usd"] = round(float(stats.get("cost_usd") or 0) + cost, 6)


def out_of_scope(
    kind: str, model, patient_id: int | None, day: date | None, options: Options | None
) -> bool:
    """Whether a valid proposal falls outside a backfill run's scope
    (module docstring, step 6). Always False for the daily analysis."""
    if options is None or not options.backfill:
        return False
    if options.patient_id is not None and patient_id not in (None, options.patient_id):
        return True
    if options.cross_check and kind in ("update_entry", "med_change"):
        return True
    if kind == "create_appointment" and options.today is not None:
        if to_local(_starts_at(model)).date() < options.today:
            return True
    if kind == "med_change" and options.meds_since is not None and day is not None:
        if day <= options.meds_since:
            return True
    return False


def add_proposals(
    db: DbSession,
    target: Target,
    proposals: list[Any],
    ctx: AnalysisContext,
    allowed_ids: set[int],
    *,
    missing_note: bool,
    options: Options | None = None,
    day: date | None = None,
    explicit_ids: set[int] | frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """Validate, scope-check (backfill), dedupe and add one call's proposals;
    returns the counts. ``explicit_ids``: the chunk's «Bot: …» messages
    (step 7); a created suggestion citing one gets the explicit rationale
    prefix, and ``answered`` holds the explicit ids cited by created ones."""
    counts: dict[str, Any] = {
        "created": 0,
        "invalid": 0,
        "duplicate": 0,
        "out_of_scope": 0,
        "explicit_created": 0,
        "explicit_skipped": 0,
        "answered": set(),
    }
    backfill = options is not None and options.backfill
    is_run = isinstance(target, AnalysisRun)
    for proposal in proposals:
        raw_ids = proposal.get("source_message_ids") if isinstance(proposal, dict) else None
        ids = [i for i in raw_ids if i in allowed_ids] if isinstance(raw_ids, list) else []
        explicit = bool(explicit_ids.intersection(ids))
        try:
            kind, payload, patient_id = to_payload(db, proposal, ctx)
            if not ids:
                raise InvalidProposal("no source messages")
            model = suggestions.parse_payload(kind, payload)
        except (InvalidProposal, SuggestionError):
            counts["invalid"] += 1
            counts["explicit_skipped"] += explicit
            continue
        if out_of_scope(kind, model, patient_id, day, options):
            counts["out_of_scope"] += 1
            counts["explicit_skipped"] += explicit
            continue
        if find_duplicate(
            db,
            kind,
            model,
            patient_id,
            include_rejected=backfill,
            entry_window_days=CROSS_CHECK_DAYS if backfill and options.cross_check else 0,
        ):
            counts["duplicate"] += 1
            counts["explicit_skipped"] += explicit
            continue
        rationale = suggestions.explicit_rationale(_s(proposal.get("rationale")), explicit)
        if missing_note:
            rationale = f"{rationale} {MISSING_NOTE}".strip()
        try:
            with db.begin_nested():
                suggestions.create_suggestion(
                    db,
                    kind=kind,
                    payload=payload,
                    rationale=rationale,
                    source_message_ids=ids,
                    source="whatsapp",
                    patient_id=patient_id,
                    import_id=None if is_run else target.id,
                    analysis_run_id=target.id if is_run else None,
                )
        except SuggestionError:
            counts["invalid"] += 1
            counts["explicit_skipped"] += explicit
            continue
        counts["created"] += 1
        if explicit:
            counts["explicit_created"] += 1
            counts["answered"].update(explicit_ids.intersection(ids))
    return counts


def analyze_day(
    db: DbSession,
    target: Target,
    day: date,
    messages: list[WhatsappMessage],
    ctx: AnalysisContext,
    client,
    options: Options,
    stats: dict[str, Any],
    unresolved_ids: set[int],
    calls: dict[str, int],
    on_call=None,
) -> None:
    """Analyze one day and fold its counts into ``stats`` (caller commits).

    ``on_call()`` runs before every Claude call (the job refreshes its lock
    there). ``calls`` counts this job's successful calls: an API error that
    will fail the same way again (400/413/422) on a job with no successful
    call yet is most likely a broken request (schema, model), so it fails
    the job instead of marking every day failed and done. Raises
    :class:`AnalysisFailed` for that, a retryable or a configuration error."""
    missing = sum(1 for m in messages if m.id in unresolved_ids)
    stats["missing_media"] += missing
    if not is_relevant(messages):
        stats["days_skipped"] += 1
        return
    lines = [(m.id, render_message(m)) for m in messages]
    chunks = chunk_lines(lines, CHUNK_MAX_CHARS)
    explicit_ids = {m.id for m in messages if is_bot_request(m.text)}
    answered: set[int] = set()
    stats["days"] += 1
    stats["explicit_requests"] += len(explicit_ids)
    failed = False
    for n, chunk in enumerate(chunks, start=1):
        header = day_header(day, n, len(chunks), missing, backfill=options.backfill)
        text = header + "\n\n" + "\n".join(t for _, t in chunk)
        if on_call is not None:
            on_call()
        result = analyze.analyze(client, options.spec, options.model, ctx.text, text)
        _add_usage(stats, result.usage, options.model)
        if result.error:
            first_api_error = result.error.startswith("api_error_") and not calls["ok"]
            if result.config_error or result.retryable or first_api_error:
                raise AnalysisFailed(
                    result.error, retryable=result.retryable, config=result.config_error
                )
            logger.warning(
                "%s analysis: %s %s, one call failed: %s",
                "backfill" if options.backfill else "daily",
                "run" if isinstance(target, AnalysisRun) else "import",
                target.id,
                result.error,
            )
            failed = True
            continue
        calls["ok"] += 1
        counts = add_proposals(
            db,
            target,
            result.proposals or [],
            ctx,
            {i for i, _ in chunk},
            missing_note=missing > 0,
            options=options,
            day=day,
            explicit_ids=explicit_ids,
        )
        stats["suggestions_created"] += counts["created"]
        stats["dropped_invalid"] += counts["invalid"]
        stats["skipped_duplicate"] += counts["duplicate"]
        stats["skipped_out_of_scope"] += counts["out_of_scope"]
        stats["explicit_created"] += counts["explicit_created"]
        stats["explicit_skipped"] += counts["explicit_skipped"]
        answered |= counts["answered"]
    if explicit_ids:
        unanswered = len(explicit_ids - answered)
        stats["explicit_unanswered"] += unanswered
        logger.info(
            "%s analysis: %s %s, day %s: %s explicit request(s), %s without a suggestion",
            "backfill" if options.backfill else "daily",
            "run" if isinstance(target, AnalysisRun) else "import",
            target.id,
            day.isoformat(),
            len(explicit_ids),
            unanswered,
        )
    if failed:
        stats["days_failed"] += 1


def run_analysis(
    db: DbSession,
    target: Target,
    messages: list[WhatsappMessage],
    client,
    options: Options,
    *,
    progress=None,
    should_stop=None,
) -> bool:
    """Analyze the days of ``messages`` not done yet, at most
    ``options.max_days``; commits after each day. Returns True when days are
    left for another job.

    Each day's transaction first takes the target's advisory lock
    (:func:`lock_target`) and re-reads it and its ``stats``, so two jobs for
    one import/run never analyze at the same time nor a day twice: the one
    that does not get the lock raises :class:`AnalysisBusy`.
    ``should_stop(target, stats)`` (backfill: stopped by an admin, cost
    limit) is asked there before each day; True ends the job at once
    (returns False; the caller tells why from the target).
    ``progress(pct, message)`` runs before every Claude call (it refreshes
    the job's ``locked_at``).
    """
    days = group_by_day(messages)
    done = set(get_stats(target, options)["days_done"])
    todo = [d for d in days if d.isoformat() not in done]
    batch = todo[: max(1, options.max_days)]
    unresolved_ids = {m.id for m in unresolved_media(db, messages)}
    calls = {"ok": 0}
    if batch:
        ctx = build_context(db, batch, today=options.today)
        if options.preamble:
            ctx.text = options.preamble + "\n\n" + ctx.text
        already = len(days) - len(todo)
        for i, day in enumerate(batch):
            stats = _locked_stats(db, target, options)
            if should_stop is not None and should_stop(target, stats):
                db.commit()
                return False
            if day.isoformat() in stats["days_done"]:
                db.commit()
                continue

            def on_call(i=i):
                if progress is None:
                    return
                if options.backfill:
                    n = already + i
                    progress(int(100 * n / len(days)), f"Analizando día {n + 1} de {len(days)}")
                else:
                    progress(int(100 * i / len(batch)), f"Analizando día {i + 1} de {len(batch)}")

            analyze_day(
                db,
                target,
                day,
                days[day],
                ctx,
                client,
                options,
                stats,
                unresolved_ids,
                calls,
                on_call,
            )
            stats["days_done"] = sorted({*stats["days_done"], day.isoformat()})
            save_stats(target, stats)
            db.commit()
    stats = _locked_stats(db, target, options)
    remaining = any(d.isoformat() not in stats["days_done"] for d in days)
    stats["status"] = "running" if remaining else "done"
    save_stats(target, stats)
    db.commit()
    return remaining


def lock_import(db: DbSession, import_id: int) -> bool:
    """Try the per-import analysis lock for the current transaction."""
    key = func.hashtextextended(f"analyze_import:{import_id}", 0)
    return bool(db.scalar(select(func.pg_try_advisory_xact_lock(key))))


def lock_target(db: DbSession, target: Target) -> bool:
    """Try the target's analysis lock (per import, or per run) for the
    current transaction."""
    if isinstance(target, AnalysisRun):
        key = func.hashtextextended(f"analysis_run:{target.id}", 0)
        return bool(db.scalar(select(func.pg_try_advisory_xact_lock(key))))
    return lock_import(db, target.id)


def _locked_stats(db: DbSession, target: Target, options: Options) -> dict[str, Any]:
    if not lock_target(db, target):
        db.rollback()
        raise AnalysisBusy()
    db.refresh(target)
    stats = get_stats(target, options)
    stats["status"] = "running"
    return stats
