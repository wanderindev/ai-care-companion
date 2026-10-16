"""LEDD over the database (ticket #133): the learned substances, the AI
classification of unknown medication names, and the admin review.

- :func:`ledd_for_items` — ``ledd.compute`` with the learned substances
  (``ledd_substances`` rows) passed in. Used by the meds API (current list,
  dry run, versions), the printed sheet and the chat.
- :func:`queue_unknown` — after a real save: every item name the catalogue
  does not know and that has no row yet gets a ``queued`` row and one
  ``classify_substance`` job (payload ``{"substance_id"}``). A name with a
  row is never sent again (``ON CONFLICT DO NOTHING`` on ``name_key``).
- ``classify_substance`` job (``app.worker.HANDLER_MODULES``): one
  ``beta.messages.create`` call (``CLASSIFICATION_MODEL``, effort ``low``,
  strict schema). The request carries **only the medication name and its
  strength as written**: never a patient, a list or a date. The answer is a
  *proposal* (``proposed``); it never counts until an admin confirms it.
  No key / 401/403/404 → the job fails, the row stays ``queued`` (re-run with
  ``python -m scripts.ledd queue``); 429/5xx/network → retried; a refused,
  truncated or invalid answer (or 400/413/422) → ``failed``, not sent again.
- :func:`confirm` / :func:`reject` (admins, audited as ``ledd_substance``).

Logs carry ids, statuses and counts only (never the name: it comes from a
patient's list).
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from app.core.config import get_settings
from app.models.ledd import CONFIDENCES, MECHANISMS, LeddSubstance
from app.services import audit, jobs, ledd
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, Usage

JOB_KIND = "classify_substance"
ENTITY = "ledd_substance"
MAX_TOKENS = 2000
EFFORT = "low"
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 1
FACTOR_MAX = 1000.0

NOT_FOUND = "Medicamento no encontrado."
NOT_REVIEWABLE = "Este medicamento ya fue revisado."
BAD_FACTOR = "El factor debe ser un número entre 0 y 1000."
NOT_CONFIGURED = "La clasificación automática de medicamentos no está configurada."
CONFIG_REFUSED = "No se pudo usar el servicio de clasificación."
RETRY_MESSAGE = "No se pudo clasificar el medicamento por ahora; se volverá a intentar."
FAILED_MESSAGE = "No se pudo clasificar el medicamento automáticamente."

logger = logging.getLogger("app.ledd")


class LeddError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Reads ────────────────────────────────────────────────────────────────────


def to_learned(row: LeddSubstance) -> ledd.Learned:
    return ledd.Learned(
        id=row.id,
        status=row.status,
        is_dopaminergic=row.is_dopaminergic,
        factor=row.factor,
        mechanism=row.mechanism,
        confidence=row.confidence,
        label=row.generic_name or row.name,
        source=row.source,
    )


def learned_map(db: DbSession) -> dict[str, ledd.Learned]:
    """Every learned substance by ``name_key`` (a small table)."""
    return {row.name_key: to_learned(row) for row in db.scalars(select(LeddSubstance))}


def ledd_for_items(
    db: DbSession, items: list[dict[str, Any]], learned: dict[str, ledd.Learned] | None = None
) -> dict[str, Any]:
    return ledd.compute(items, learned if learned is not None else learned_map(db))


def unknown_items(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Items the catalogue does not know, by ``name_key`` (first item wins)."""
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        key = ledd.name_key(item.get("name"))
        if key and key not in out and ledd.match_substance(item) is None:
            out[key] = item
    return out


# ── Queue ────────────────────────────────────────────────────────────────────


def queue_unknown(db: DbSession, items: list[dict[str, Any]]) -> int:
    """Create a ``queued`` row + job for each unknown name without a row, in
    the caller's transaction (the caller commits). Returns how many."""
    unknown = unknown_items(items)
    if not unknown:
        return 0
    queued = 0
    for key, item in unknown.items():
        new_id = db.scalar(
            pg_insert(LeddSubstance)
            .values(
                name_key=key,
                name=(item.get("name") or key)[:200],
                strength=(item.get("strength") or None),
                status="queued",
            )
            .on_conflict_do_nothing(index_elements=["name_key"])
            .returning(LeddSubstance.id)
        )
        if new_id is None:
            continue
        jobs.enqueue(db, JOB_KIND, {"substance_id": new_id}, message="En cola")
        queued += 1
    if queued:
        logger.info("ledd: %s unknown medication name(s) queued for classification", queued)
    return queued


def requeue_stuck(db: DbSession) -> int:
    """Queue a job for every row still ``queued`` (after the key is set)."""
    ids = list(db.scalars(select(LeddSubstance.id).where(LeddSubstance.status == "queued")))
    for substance_id in ids:
        jobs.enqueue(db, JOB_KIND, {"substance_id": substance_id}, message="En cola")
    return len(ids)


# ── Claude call ──────────────────────────────────────────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_dopaminergic",
        "mechanism",
        "factor",
        "generic_name",
        "source",
        "confidence",
    ],
    "properties": {
        "is_dopaminergic": {"type": "boolean"},
        "mechanism": {"type": "string", "enum": list(MECHANISMS)},
        "factor": {"type": "number"},
        "generic_name": {"type": "string"},
        "source": {"type": "string"},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
    },
}

SYSTEM_PROMPT = """\
You classify ONE medication name, as written on a Spanish-language (Panamá) \
medication list, for the Levodopa Equivalent Daily Dose (LEDD) used in \
Parkinson's disease research. You receive only the name and, sometimes, the \
strength as written. Answer in the given JSON schema.

- generic_name: the active ingredient(s) in Spanish (e.g. "pramipexol"), or \
the name as given if you do not recognise it.
- is_dopaminergic: true only if the drug counts toward the LEDD (levodopa in \
any formulation, COMT inhibitors, dopamine agonists, MAO-B inhibitors, \
amantadine and similar). Dopamine ANTAGONISTS (haloperidol, metoclopramide, \
antipsychotics) are NOT dopaminergic for the LEDD: false. Antidepressants, \
benzodiazepines, pregabalin, antihypertensives, vitamins, etc.: false.
- mechanism: the class; "not_dopaminergic" when is_dopaminergic is false, \
"unknown" when you do not recognise the name.
- factor: the conversion factor per mg/day of the drug from Jost et al., \
Mov Disord 2023 (else Tomlinson et al., Mov Disord 2010); for a COMT \
inhibitor, the ratio applied to the levodopa dose (e.g. 0.33). 0 when not \
dopaminergic or unknown.
- source: the reference of the factor ("Jost 2023", "Tomlinson 2010") or \
"ninguna".
- confidence: "high" only when you are sure of the drug and the factor."""


def make_client(api_key: str):
    """The real Anthropic client (tests replace it with a fake)."""
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
    )


def request_params(model: str, name: str, strength: str | None) -> dict[str, Any]:
    text = f"Medicamento: {name}"
    if strength:
        text += f"\nConcentración: {strength}"
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def normalize_answer(data: Any) -> dict[str, Any] | None:
    """The validated proposal, or None when it does not match the schema."""
    if not isinstance(data, dict):
        return None
    dopa = data.get("is_dopaminergic")
    mechanism = data.get("mechanism")
    factor = data.get("factor")
    confidence = data.get("confidence")
    if not isinstance(dopa, bool) or mechanism not in MECHANISMS or confidence not in CONFIDENCES:
        return None
    if isinstance(factor, bool) or not isinstance(factor, int | float):
        return None
    if not 0 <= factor <= FACTOR_MAX:
        return None
    generic = data.get("generic_name") if isinstance(data.get("generic_name"), str) else ""
    source = data.get("source") if isinstance(data.get("source"), str) else ""
    return {
        "is_dopaminergic": dopa,
        "mechanism": mechanism,
        "factor": float(factor) if dopa else 0.0,
        "generic_name": " ".join(generic.split())[:200] or None,
        "source": " ".join(source.split())[:300] or None,
        "confidence": confidence,
    }


PERMANENT_STATUSES = frozenset({400, 413, 422})
CONFIG_STATUSES = frozenset({401, 403, 404})


@jobs.handler(JOB_KIND)
def run_classify_substance(ctx: jobs.JobContext) -> None:
    import anthropic

    substance_id = ctx.payload.get("substance_id")
    if not isinstance(substance_id, int) or isinstance(substance_id, bool):
        raise jobs.PermanentJobError("Tarea de clasificación inválida.")
    db = ctx.db
    row = db.get(LeddSubstance, substance_id, with_for_update=True)
    if row is None:
        raise jobs.PermanentJobError(NOT_FOUND)
    if row.status != "queued":  # a second job for the same name: never sent twice
        ctx.progress(100, "Este medicamento ya se había clasificado")
        return
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise jobs.PermanentJobError(NOT_CONFIGURED)
    model = settings.classification_model
    usage = Usage()
    ctx.progress(10, "Clasificando el medicamento")
    try:
        response = make_client(settings.anthropic_api_key).beta.messages.create(
            **request_params(model, row.name, row.strength)
        )
    except anthropic.APIStatusError as exc:
        if exc.status_code in CONFIG_STATUSES:
            raise jobs.PermanentJobError(CONFIG_REFUSED) from None
        if exc.status_code in PERMANENT_STATUSES:
            _fail(db, row, f"api_error_{exc.status_code}", model, usage)
            return
        raise jobs.JobError(RETRY_MESSAGE) from None
    except anthropic.APIConnectionError:
        raise jobs.JobError(RETRY_MESSAGE) from None
    usage.add(response.usage)
    if response.stop_reason in ("refusal", "max_tokens"):
        _fail(
            db, row, "refusal" if response.stop_reason == "refusal" else "truncated", model, usage
        )
        return
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    try:
        answer = normalize_answer(json.loads(text) if text else None)
    except json.JSONDecodeError:
        answer = None
    if answer is None:
        _fail(db, row, "invalid_json", model, usage)
        return
    for field, value in answer.items():
        setattr(row, field, value)
    row.status = "proposed"
    row.model = model
    row.usage = usage.as_dict()
    db.commit()
    logger.info(
        "ledd: substance %s proposed (dopaminergic=%s, confidence=%s, tokens in=%s out=%s)",
        row.id,
        answer["is_dopaminergic"],
        answer["confidence"],
        usage.input_tokens,
        usage.output_tokens,
    )
    ctx.progress(100, "Clasificado; pendiente de revisión")


def _fail(db: DbSession, row: LeddSubstance, code: str, model: str, usage: Usage) -> None:
    row.status = "failed"
    row.error = code
    row.model = model
    row.usage = usage.as_dict()
    db.commit()
    logger.warning("ledd: substance %s classification failed: %s", row.id, code)
    raise jobs.PermanentJobError(FAILED_MESSAGE)


# ── Review (admins) ──────────────────────────────────────────────────────────


def get_substance(db: DbSession, substance_id: int, *, lock: bool = False) -> LeddSubstance:
    row = db.get(LeddSubstance, substance_id, with_for_update=lock)
    if row is None:
        raise LeddError(NOT_FOUND, status=404)
    return row


def list_substances(db: DbSession, *, review_only: bool = False) -> list[LeddSubstance]:
    rows = list(db.scalars(select(LeddSubstance).order_by(LeddSubstance.name_key)))
    if review_only:
        rows = [r for r in rows if ledd.needs_review(to_learned(r))]
    return rows


def _decide(
    db: DbSession,
    substance_id: int,
    values: dict[str, Any],
    *,
    action: str,
    user_id: int | None,
) -> LeddSubstance:
    row = get_substance(db, substance_id, lock=True)
    if row.status not in ("proposed", "failed", "queued"):
        raise LeddError(NOT_REVIEWABLE, status=409)
    before = audit.snapshot(row)
    for field, value in values.items():
        setattr(row, field, value)
    row.reviewed_by = user_id
    row.reviewed_at = datetime.now(UTC)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=action,
        entity=ENTITY,
        entity_id=row.id,
        before=before,
        after=audit.snapshot(row),
    )
    return row


def confirm(
    db: DbSession, substance_id: int, *, factor: float | None = None, user_id: int | None
) -> LeddSubstance:
    """Accept the proposal as dopaminergic with its factor (or ``factor``)."""
    row = get_substance(db, substance_id)
    value = factor if factor is not None else row.factor
    if value is None or not 0 < float(value) <= FACTOR_MAX:
        raise LeddError(BAD_FACTOR, status=422)
    mechanism = row.mechanism
    if mechanism in (None, "not_dopaminergic", "unknown"):
        mechanism = "other_dopaminergic"
    return _decide(
        db,
        substance_id,
        {
            "status": "confirmed",
            "is_dopaminergic": True,
            "factor": float(value),
            "mechanism": mechanism,
        },
        action=audit.ACTION_APPROVE,
        user_id=user_id,
    )


def reject(db: DbSession, substance_id: int, *, user_id: int | None) -> LeddSubstance:
    """It does not count toward the LEDD (adds 0)."""
    return _decide(
        db,
        substance_id,
        {"status": "rejected", "is_dopaminergic": False},
        action=audit.ACTION_REJECT,
        user_id=user_id,
    )
