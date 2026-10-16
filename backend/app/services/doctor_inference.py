"""Infer the missing doctor of bitácora entries with Claude (ticket #125).

The WhatsApp analysis (#39, #40) sets ``doctor_id`` only when a message names
the doctor, so a rebuilt history has many consultations, exams and therapies
without one. Often the doctor follows from the specialty the text implies,
the patient's nearby entries and appointments, and the doctor directory.
This module asks Claude (``app/prompts/doctor_inference_v1``) and turns only
**high-confidence** answers naming an **active** doctor into ``update_entry``
suggestions (``changes: {doctor_id}``, ``source="ai"``, the reason in
``rationale``). Nothing is applied here: approval in «Pendientes» goes
through ``suggestions.approve`` → ``log_entries.update_entry`` (validation
and audit as a manual edit), and refuses once the entry has a doctor.

- **Candidates** (:func:`candidates`): one patient's entries with
  ``doctor_id IS NULL`` and an ``entry_type`` in :data:`ENTRY_TYPES`
  (never ``observacion_familiar``, ``hospitalizacion`` or ``otro``), in an
  optional date range, minus entries that already have a suggestion from
  this feature (any status: a rejected one is never re-proposed) or a
  pending suggestion of any producer proposing a ``doctor_id``.
- **Context per batch** (:func:`batch_text`, ``BATCH_SIZE`` entries): the
  candidates (id, date, type, title, short excerpts of indications and
  family notes); the patient's entries **with** a doctor within
  ±``WINDOW_DAYS`` of the batch (date, type, doctor id, title); the
  patient's non-cancelled appointments in that window with a doctor (date,
  doctor id, specialty; never notes). The active doctors (id, name,
  aliases, specialty) are a cached block shared by every call. Never a
  patient's name, ID number or birth date.
- **Calls**: ``beta.messages.create``, ``ANALYSIS_MODEL``, strict schema.
  Progress is reported before each call, so the worst case of one call
  (``REQUEST_TIMEOUT_SECONDS`` x (1 + ``MAX_RETRIES``)) must stay under
  ``jobs.LOCK_TIMEOUT`` (tested). No transaction is held across a call.
- **Idempotent**: a suggestion is created only after re-reading (locked) the
  entry: still no doctor, still not proposed. A retried job continues where
  it stopped.

Logs and job messages carry ids, counts and token usage only.
"""

import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Integer, and_, or_, select
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.models.appointment import Appointment
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.log_entry import LogEntry
from app.models.patient import Patient
from app.models.suggestion import SOURCE_AI, STATUS_PENDING, Suggestion
from app.prompts import doctor_inference_v1 as prompt
from app.services import jobs, suggestions
from app.services.appointments import local_datetime, to_local
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, PRICES, Usage
from app.services.jobs import JobError, PermanentJobError

logger = logging.getLogger(__name__)

SPEC = prompt.SPEC
JOB_KIND = "infer_doctors"
# Entry types that normally have a doctor.
ENTRY_TYPES = (
    "consulta_control",
    "consulta_enfermedad",
    "teleconsulta",
    "examen",
    "procedimiento",
    "terapia",
    "vacuna",
    "visita_domiciliaria",
)
BATCH_SIZE = 20
WINDOW_DAYS = 60
TITLE_CHARS = 120
EXCERPT_CHARS = 240
REASON_CHARS = 300
MAX_TOKENS = 16000
EFFORT = "medium"
# Progress (which refreshes the job's ``locked_at``) is reported only before
# each call, so one call's worst case must stay under ``jobs.LOCK_TIMEOUT``
# (15 min) or stuck-job recovery would start a second, paid, copy of the run:
# REQUEST_TIMEOUT_SECONDS x (1 + MAX_RETRIES) + the SDK's backoff = about
# 10 min (``test_request_worst_case_fits_job_lock``).
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 1
# Rough estimate inputs.
CHARS_PER_TOKEN = 3.0
OUTPUT_TOKENS_PER_ENTRY = 80
OUTPUT_TOKENS_PER_BATCH = 1500


class InferenceError(ValueError):
    """Bad request parameters; Spanish message, ``status`` for the API."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ── candidates ──────────────────────────────────────────────────────────────


def proposed_entry_ids(db: DbSession, patient_id: int) -> set[int]:
    """Entries never to send: those with a suggestion from this feature (any
    status) or a pending ``update_entry`` of any producer proposing a doctor."""
    entry_id = Suggestion.payload["entry_id"].astext.cast(Integer)
    rows = db.scalars(
        select(entry_id).where(
            Suggestion.kind == "update_entry",
            Suggestion.patient_id == patient_id,
            or_(
                Suggestion.source == SOURCE_AI,
                and_(
                    Suggestion.status == STATUS_PENDING,
                    Suggestion.payload["changes"].has_key("doctor_id"),
                ),
            ),
        )
    )
    return {i for i in rows if i is not None}


def candidates(
    db: DbSession, patient_id: int, date_from: date | None = None, date_to: date | None = None
) -> list[LogEntry]:
    stmt = select(LogEntry).where(
        LogEntry.patient_id == patient_id,
        LogEntry.doctor_id.is_(None),
        LogEntry.entry_type.in_(ENTRY_TYPES),
    )
    if date_from is not None:
        stmt = stmt.where(LogEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(LogEntry.entry_date <= date_to)
    skip = proposed_entry_ids(db, patient_id)
    rows = db.scalars(stmt.order_by(LogEntry.entry_date, LogEntry.id))
    return [e for e in rows if e.id not in skip]


def check_params(
    db: DbSession, patient_id: int, date_from: date | None, date_to: date | None
) -> None:
    if db.get(Patient, patient_id) is None:
        raise InferenceError("El paciente no existe.", status=404)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise InferenceError("La fecha inicial es posterior a la final.")


# ── what Claude sees ────────────────────────────────────────────────────────


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def active_doctors(db: DbSession) -> dict[int, Doctor]:
    rows = db.scalars(select(Doctor).where(Doctor.is_active.is_(True)).order_by(Doctor.name))
    return {d.id: d for d in rows}


def doctors_text(doctors: Iterable[Doctor]) -> str:
    lines = ["DIRECTORIO DE MÉDICOS ACTIVOS (id · nombre · alias · especialidad)"]
    for d in doctors:
        aliases = ", ".join(a for a in (d.aliases or []) if a)
        lines.append(f"- {d.id} · {d.name} · {aliases or '-'} · {d.specialty or '-'}")
    if len(lines) == 1:
        lines.append("- (ninguno)")
    return "\n".join(lines)


def window(batch: list[LogEntry]) -> tuple[date, date]:
    days = [e.entry_date for e in batch]
    return min(days) - timedelta(days=WINDOW_DAYS), max(days) + timedelta(days=WINDOW_DAYS)


def batch_text(db: DbSession, patient_id: int, batch: list[LogEntry]) -> str:
    lo, hi = window(batch)
    lines = [
        "ENTRADAS SIN MÉDICO (entry_id · fecha · tipo · título · indicaciones · notas)",
    ]
    for e in batch:
        lines.append(
            f"- [entry {e.id}] · {e.entry_date.isoformat()} · {e.entry_type} · "
            f"{_clip(e.title or e.source_label, TITLE_CHARS) or '-'} · "
            f"{_clip(e.indications, EXCERPT_CHARS) or '-'} · "
            f"{_clip(e.family_notes, EXCERPT_CHARS) or '-'}"
        )
    nearby = db.scalars(
        select(LogEntry)
        .where(
            LogEntry.patient_id == patient_id,
            LogEntry.doctor_id.is_not(None),
            LogEntry.entry_date.between(lo, hi),
        )
        .order_by(LogEntry.entry_date, LogEntry.id)
    )
    lines += ["", "ENTRADAS CERCANAS CON MÉDICO (fecha · tipo · doctor_id · título)"]
    count = len(lines)
    for e in nearby:
        lines.append(
            f"- {e.entry_date.isoformat()} · {e.entry_type} · {e.doctor_id} · "
            f"{_clip(e.title or e.source_label, TITLE_CHARS) or '-'}"
        )
    if len(lines) == count:
        lines.append("- (ninguna)")
    appts = db.scalars(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.doctor_id.is_not(None),
            Appointment.status != "cancelada",
            Appointment.starts_at >= local_datetime(lo),
            Appointment.starts_at < local_datetime(hi + timedelta(days=1)),
        )
        .order_by(Appointment.starts_at, Appointment.id)
    )
    lines += ["", "CITAS CERCANAS (fecha · doctor_id · especialidad)"]
    count = len(lines)
    for a in appts:
        specialty = a.doctor.specialty if a.doctor else None
        lines.append(
            f"- {to_local(a.starts_at).date().isoformat()} · {a.doctor_id} · {specialty or '-'}"
        )
    if len(lines) == count:
        lines.append("- (ninguna)")
    return "\n".join(lines)


# ── estimate ────────────────────────────────────────────────────────────────


@dataclass
class Estimate:
    candidates: int
    batches: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    model: str
    cost_usd: float | None


def _batches(rows: list[LogEntry]) -> list[list[LogEntry]]:
    return [rows[i : i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]


def estimate(
    db: DbSession,
    settings: Settings,
    patient_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Estimate:
    """A rough estimate (characters → tokens) of a run's calls and cost."""
    check_params(db, patient_id, date_from, date_to)
    model = settings.analysis_model
    rows = candidates(db, patient_id, date_from, date_to)
    batches = _batches(rows)
    doctors_block = doctors_text(active_doctors(db).values())
    cached = int((len(SPEC.system) + len(doctors_block)) / CHARS_PER_TOKEN)
    fresh = sum(int(len(batch_text(db, patient_id, b)) / CHARS_PER_TOKEN) for b in batches)
    output = len(rows) * OUTPUT_TOKENS_PER_ENTRY + len(batches) * OUTPUT_TOKENS_PER_BATCH
    cost: float | None = 0.0
    if batches:
        cost = None
        if model in PRICES:
            p_in, p_out, p_write, p_read = PRICES[model]
            cost = round(
                (
                    fresh * p_in
                    + output * p_out
                    + cached * p_write
                    + cached * (len(batches) - 1) * p_read
                )
                / 1_000_000,
                2,
            )
    return Estimate(len(rows), len(batches), fresh, cached, output, model, cost)


def active_job(db: DbSession) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.kind == JOB_KIND, Job.status.in_(("queued", "running")))
        .order_by(Job.id)
        .limit(1)
    )


# ── the Claude call ─────────────────────────────────────────────────────────


@dataclass
class Answer:
    entry_id: int
    doctor_id: int  # 0 = unsure
    confidence: str
    reason: str


@dataclass
class BatchAnswer:
    answers: dict[int, Answer] = field(default_factory=dict)
    error: str | None = None  # short code, never model text
    usage: Usage = field(default_factory=Usage)


def request_params(model: str, doctors_block: str, batch: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SPEC.system, "cache_control": {"type": "ephemeral"}}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": SPEC.schema},
        },
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": doctors_block, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": batch},
                ],
            }
        ],
    }
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def make_client(api_key: str):
    """The real Anthropic client (tests replace it with a fake whose
    ``beta.messages.create`` answers)."""
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
    )


def parse_answers(data: Any, batch_ids: set[int]) -> dict[int, Answer] | None:
    """Answers by entry id; ``None`` when the answer is not the schema's shape.
    Items for entries outside the batch, repeats and bad enums are dropped."""
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    out: dict[int, Answer] = {}
    for item in data["entries"]:
        if not isinstance(item, dict):
            continue
        entry_id = item.get("entry_id")
        if entry_id not in batch_ids or entry_id in out:
            continue
        doctor_id = item.get("doctor_id")
        confidence = item.get("confidence")
        if not isinstance(doctor_id, int) or confidence not in prompt.CONFIDENCES:
            continue
        reason = _clip(str(item.get("reason") or ""), REASON_CHARS)
        out[entry_id] = Answer(entry_id, max(doctor_id, 0), confidence, reason)
    return out


def ask(client, model: str, doctors_block: str, batch_ids: Iterable[int], text: str) -> BatchAnswer:
    """One batch. Transient API trouble raises ``JobError`` (retry later), a
    bad key or model ``PermanentJobError``; a refused, truncated or malformed
    answer is a ``BatchAnswer.error`` (the batch gets no suggestion)."""
    import anthropic

    answer = BatchAnswer()
    try:
        response = client.beta.messages.create(**request_params(model, doctors_block, text))
    except anthropic.APIStatusError as exc:
        if exc.status_code in (401, 403, 404):
            raise PermanentJobError(
                "Claude rechazó la clave o el modelo configurado; revisa la configuración."
            ) from None
        if exc.status_code in (400, 413):
            answer.error = f"api_error_{exc.status_code}"
            return answer
        raise JobError("Claude no respondió; se volverá a intentar.") from None
    except anthropic.APIConnectionError:
        raise JobError("Claude no respondió; se volverá a intentar.") from None
    answer.usage.add(response.usage)
    if response.stop_reason in ("refusal", "max_tokens"):
        answer.error = "refusal" if response.stop_reason == "refusal" else "truncated"
        return answer
    raw = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        data = None
    answers = parse_answers(data, set(batch_ids))
    if answers is None:
        answer.error = "invalid_json"
        return answer
    answer.answers = answers
    return answer


# ── the run ─────────────────────────────────────────────────────────────────


@dataclass
class InferenceStats:
    candidates: int = 0
    batches: int = 0
    answers: Counter = field(default_factory=Counter)  # by confidence
    unsure: int = 0  # doctor_id 0
    invalid_doctor: int = 0  # unknown or inactive id
    suggestions: int = 0
    skipped: int = 0  # got a doctor or a proposal meanwhile
    errors: Counter = field(default_factory=Counter)
    usage: Usage = field(default_factory=Usage)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "batches": self.batches,
            "answers": dict(self.answers),
            "unsure": self.unsure,
            "invalid_doctor": self.invalid_doctor,
            "suggestions": self.suggestions,
            "skipped": self.skipped,
            "errors": dict(self.errors),
            "usage": self.usage.as_dict(),
        }


def run_inference(
    db: DbSession,
    settings: Settings,
    patient_id: int,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    client=None,
    user_id: int | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> InferenceStats:
    """Ask Claude about every candidate entry, batch by batch, and store the
    high-confidence answers as suggestions (see the module docstring)."""
    check_params(db, patient_id, date_from, date_to)
    if client is None:
        if not settings.anthropic_api_key:
            raise PermanentJobError("Falta la clave de Claude (ANTHROPIC_API_KEY).")
        client = make_client(settings.anthropic_api_key)
    model = settings.analysis_model
    ids = [e.id for e in candidates(db, patient_id, date_from, date_to)]
    stats = InferenceStats(candidates=len(ids))
    total = max(1, -(-len(ids) // BATCH_SIZE))
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = set(ids[start : start + BATCH_SIZE])
        # Re-read: an entry may have got a doctor or a proposal meanwhile.
        batch = [e for e in candidates(db, patient_id, date_from, date_to) if e.id in chunk]
        stats.skipped += len(chunk) - len(batch)
        if not batch:
            continue
        doctors = active_doctors(db)
        block = doctors_text(doctors.values())
        text = batch_text(db, patient_id, batch)
        batch_ids = [e.id for e in batch]
        if progress is not None:
            progress(
                int(100 * stats.batches / total),
                f"Buscando médicos con IA ({stats.batches + 1} de {total})",
            )
        db.rollback()  # no transaction (or connection) held across the call
        answer = ask(client, model, block, batch_ids, text)
        stats.batches += 1
        if answer.usage.requests:  # a 400/413 billed nothing
            stats.usage.add(answer.usage)
        logger.info(
            "doctor inference batch: patient=%s entries=%d answers=%d error=%s "
            "input_tokens=%d output_tokens=%d cache_read=%d",
            patient_id,
            len(batch_ids),
            len(answer.answers),
            answer.error,
            answer.usage.input_tokens,
            answer.usage.output_tokens,
            answer.usage.cache_read_input_tokens,
        )
        if answer.error:
            stats.errors[answer.error] += 1
            continue
        stats.errors.update({"missing": len(batch_ids) - len(answer.answers)})
        _store(db, patient_id, answer, set(doctors), stats, user_id=user_id)
    stats.errors = +stats.errors
    return stats


def _store(db, patient_id, answer: BatchAnswer, doctor_ids, stats, *, user_id) -> None:
    for entry_id, item in sorted(answer.answers.items()):
        stats.answers[item.confidence] += 1
        if item.doctor_id == 0:
            stats.unsure += 1
            continue
        if item.doctor_id not in doctor_ids:
            stats.invalid_doctor += 1
            continue
        if item.confidence != "high":
            continue
        entry = db.get(LogEntry, entry_id, with_for_update=True, populate_existing=True)
        if (
            entry is None
            or entry.doctor_id is not None
            or entry.entry_type not in ENTRY_TYPES
            or entry_id in proposed_entry_ids(db, patient_id)
        ):
            stats.skipped += 1
            continue
        try:
            with db.begin_nested():
                suggestions.create_suggestion(
                    db,
                    kind="update_entry",
                    payload={"entry_id": entry_id, "changes": {"doctor_id": item.doctor_id}},
                    rationale=item.reason or None,
                    source=SOURCE_AI,
                    patient_id=patient_id,
                    created_by=user_id,
                )
        except suggestions.SuggestionError:
            stats.invalid_doctor += 1  # deactivated meanwhile
            continue
        stats.suggestions += 1
    db.commit()


# ── job ─────────────────────────────────────────────────────────────────────


def _day(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def job_payload(
    patient_id: int, date_from: date | None, date_to: date | None, user_id: int | None
) -> dict[str, Any]:
    return {
        "patient_id": patient_id,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "user_id": user_id,
    }


@jobs.handler(JOB_KIND)
def run_infer_doctors(ctx: jobs.JobContext) -> None:
    payload = ctx.payload
    try:
        patient_id = int(payload["patient_id"])
        date_from, date_to = _day(payload.get("date_from")), _day(payload.get("date_to"))
    except (KeyError, TypeError, ValueError):
        raise PermanentJobError("Datos del trabajo no válidos.") from None
    user_id = payload.get("user_id")
    try:
        stats = run_inference(
            ctx.db,
            get_settings(),
            patient_id,
            date_from=date_from,
            date_to=date_to,
            user_id=int(user_id) if user_id else None,
            progress=ctx.progress,
        )
    except InferenceError as exc:
        raise PermanentJobError(str(exc)) from None
    usage = stats.usage.as_dict()
    logger.info(
        "doctor inference job %s: candidates=%d batches=%d suggestions=%d unsure=%d "
        "invalid=%d errors=%d input_tokens=%d output_tokens=%d",
        ctx.job_id,
        stats.candidates,
        stats.batches,
        stats.suggestions,
        stats.unsure,
        stats.invalid_doctor,
        sum(stats.errors.values()),
        usage["input_tokens"],
        usage["output_tokens"],
    )
    n = stats.suggestions
    ctx.progress(
        100,
        "Listo: 1 sugerencia de médico" if n == 1 else f"Listo: {n} sugerencias de médico",
    )
