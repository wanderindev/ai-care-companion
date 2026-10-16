"""LLM triage of the analytes flagged for review (ticket #100).

The lab extraction creates an analyte with ``needs_review`` for every test
name it does not know (``matching.AnalyteMatcher``); most are another
spelling, abbreviation or translation of an analyte already in the catalogue,
the rest are genuinely new. Reviewing hundreds by hand is not realistic, so
Claude proposes, per analyte, one of (``app/prompts/analyte_triage_v1``):

- ``merge_into`` a reviewed analyte (``target_id``),
- ``keep_as_new`` (optionally with a cleaned ``display_name``),
- ``unsure``,

with a ``confidence`` and a ``reason_code``. The request carries only the
analytes' names, aliases, specimen, units seen and a few example values as
printed (``analytes.stats``) — never a patient, a date, a lab or a report.

The proposal is stored on the analyte (``analytes.triage``, migration 0027)::

    {"version", "model", "action", "target_id", "display_name",
     "confidence", "reason_code", "units_match",
     "status": "pending" | "applied" | "accepted" | "rejected",
     "error": short code | null}

Nothing is applied by the triage itself, except — only when the operator
passes ``--apply`` to the CLI (``run_triage(apply=True)``) — the safe cases
(``auto_applicable``): a ``high`` confidence ``keep_as_new``, and a ``high``
confidence ``merge_into`` a target of the same specimen whose units cover
the source's (a source with no unit at all is ``units_match`` null and
never auto-applied). Everything else waits on «Revisar exámenes» for an admin to
accept (``apply_proposal``) or reject. Applying always goes through
``analytes.merge_analytes`` / ``analytes.update_analyte`` (same validation,
specimen rule and audit as the manual buttons); a merge deletes the source
analyte, and its proposal with it.

Batches: ``BATCH_SIZE`` analytes per call; the reviewed catalogue is a cached
block shared by every call. Each batch's analytes are read, the transaction
is ended (no connection held across the Claude call), then the proposals are
written only for analytes still ``needs_review`` without a proposal — so a
retried job, or a second run, continues where the last one stopped and an
analyte someone reviewed meanwhile is left alone. Analytes with a proposal
(even a rejected one) are never sent again; clear ``triage`` to redo one.

Logs carry ids, counts, outcome codes and token counts only.
"""

import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings
from app.models.job import Job
from app.models.lab import Analyte, LabResult
from app.prompts import analyte_triage_v1 as prompt
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, PRICES, Usage
from app.services.jobs import JobError, PermanentJobError
from app.services.labs import analytes as analyte_svc

logger = logging.getLogger(__name__)

SPEC = prompt.SPEC
JOB_KIND = "analyte_triage"
BATCH_SIZE = 30
MAX_TOKENS = 16000
EFFORT = "medium"
# Progress (which refreshes the job's ``locked_at``) is reported only before
# each call, so one call's worst case must stay under ``jobs.LOCK_TIMEOUT``
# (15 min) or stuck-job recovery would start a second, paid, copy of the run:
# REQUEST_TIMEOUT_SECONDS x (1 + MAX_RETRIES) + the SDK's backoff (a few
# seconds) = about 10 min. A call still failing after that raises
# ``JobError`` and the job retries later, continuing where it stopped
# (``test_request_worst_case_fits_job_lock`` checks the bound).
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 1
UNITS_PER_ANALYTE = 5
EXAMPLE_CHARS = 60
# Rough estimate inputs: characters per token for this mixed Spanish/number
# text, and output tokens per proposal (+ thinking) per batch.
CHARS_PER_TOKEN = 3.0
OUTPUT_TOKENS_PER_ANALYTE = 60
OUTPUT_TOKENS_PER_BATCH = 1500

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"  # by ``--apply``
STATUS_ACCEPTED = "accepted"  # by an admin on the page
STATUS_REJECTED = "rejected"


# ── what Claude sees ────────────────────────────────────────────────────────


def normalize_unit(unit: str | None) -> str:
    return "".join((unit or "").split()).casefold().replace("µ", "u").replace("μ", "u")


def units_seen(db: DbSession, analyte_ids: Iterable[int]) -> dict[int, list[str]]:
    """The units printed on each analyte's results, most frequent first."""
    ids = list(analyte_ids)
    out: dict[int, list[str]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = db.execute(
        select(LabResult.analyte_id, LabResult.unit, func.count().label("n"))
        .where(LabResult.analyte_id.in_(ids), LabResult.unit.is_not(None), LabResult.unit != "")
        .group_by(LabResult.analyte_id, LabResult.unit)
        .order_by(LabResult.analyte_id, func.count().desc(), LabResult.unit)
    )
    for analyte_id, unit, _n in rows:
        if len(out[analyte_id]) < UNITS_PER_ANALYTE:
            out[analyte_id].append(unit)
    return out


def _units(analyte: Analyte, seen: list[str]) -> list[str]:
    units = [analyte.default_unit] if analyte.default_unit else []
    keys = {normalize_unit(u) for u in units}
    for unit in seen:
        if normalize_unit(unit) not in keys:
            keys.add(normalize_unit(unit))
            units.append(unit)
    return units


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXAMPLE_CHARS else text[: EXAMPLE_CHARS - 1] + "…"


def describe(analyte: Analyte, units: list[str], examples: list[dict] | None = None) -> str:
    parts = [f"[id {analyte.id}] {analyte.canonical_name}"]
    if analyte.aliases:
        parts.append("alias: " + "; ".join(analyte.aliases))
    parts.append(f"muestra: {analyte.specimen}")
    parts.append("unidades: " + (", ".join(units) if units else "(ninguna)"))
    if examples is not None:
        shown = [
            f"{_clip(e['raw_name'])} = {_clip(e['value_text']) or '—'}"
            + (f" {_clip(e['unit'])}" if e.get("unit") else "")
            for e in examples
        ]
        parts.append("ejemplos: " + ("; ".join(shown) if shown else "(ninguno)"))
    return " | ".join(parts)


def catalogue(db: DbSession) -> tuple[list[Analyte], str]:
    """The reviewed analytes and their text block (the cached context)."""
    rows = list(
        db.scalars(select(Analyte).where(Analyte.needs_review.is_(False)).order_by(Analyte.id))
    )
    seen = units_seen(db, [a.id for a in rows])
    lines = [describe(a, _units(a, seen[a.id])) for a in rows]
    text = "CATÁLOGO (exámenes ya revisados):\n" + ("\n".join(lines) if lines else "(vacío)")
    return rows, text


def batch_text(db: DbSession, batch: list[Analyte]) -> str:
    ids = [a.id for a in batch]
    seen = units_seen(db, ids)
    stats = analyte_svc.stats(db, ids)
    lines = [describe(a, _units(a, seen[a.id]), stats[a.id].examples) for a in batch]
    return "EXÁMENES POR REVISAR:\n" + "\n".join(lines)


def pending_query(after_id: int = 0):
    return (
        select(Analyte)
        .where(Analyte.needs_review.is_(True), Analyte.triage.is_(None), Analyte.id > after_id)
        .order_by(Analyte.id)
    )


def pending_count(db: DbSession) -> int:
    return db.scalar(select(func.count()).select_from(pending_query().subquery())) or 0


# ── estimate ────────────────────────────────────────────────────────────────


@dataclass
class Estimate:
    analytes: int
    batches: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    model: str
    cost_usd: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "analytes": self.analytes,
            "batches": self.batches,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "cost_usd": self.cost_usd,
        }


def estimate(db: DbSession, settings: Settings) -> Estimate:
    """A rough upper estimate (characters → tokens) of a full run's cost."""
    model = settings.extraction_model
    pending = list(db.scalars(pending_query()))
    batches = [pending[i : i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    _, cat_text = catalogue(db)
    cached = int((len(SPEC.system) + len(cat_text)) / CHARS_PER_TOKEN)
    fresh = sum(int(len(batch_text(db, b)) / CHARS_PER_TOKEN) for b in batches)
    output = len(pending) * OUTPUT_TOKENS_PER_ANALYTE + len(batches) * OUTPUT_TOKENS_PER_BATCH
    cost = None
    if model in PRICES and batches:
        p_in, p_out, p_write, p_read = PRICES[model]
        cost = (
            fresh * p_in + output * p_out + cached * p_write + cached * (len(batches) - 1) * p_read
        ) / 1_000_000
        cost = round(cost, 2)
    elif not batches:
        cost = 0.0
    return Estimate(len(pending), len(batches), fresh, cached, output, model, cost)


# ── the Claude call ─────────────────────────────────────────────────────────


@dataclass
class Proposal:
    analyte_id: int
    action: str
    target_id: int | None
    display_name: str | None
    confidence: str
    reason_code: str | None


@dataclass
class BatchAnswer:
    proposals: dict[int, Proposal] = field(default_factory=dict)
    error: str | None = None  # short code, never model text
    usage: Usage = field(default_factory=Usage)


def request_params(model: str, catalogue_text: str, batch: str) -> dict[str, Any]:
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
                    {
                        "type": "text",
                        "text": catalogue_text,
                        "cache_control": {"type": "ephemeral"},
                    },
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


def parse_proposals(
    data: Any, batch_ids: set[int], targets: dict[int, Analyte]
) -> dict[int, Proposal] | None:
    """Validated proposals by analyte id; ``None`` when the answer is not the
    schema's shape. A merge into an unknown or non-catalogue analyte becomes
    ``unsure`` (``reason_code`` ``invalid_target``)."""
    if not isinstance(data, dict) or not isinstance(data.get("proposals"), list):
        return None
    out: dict[int, Proposal] = {}
    for item in data["proposals"]:
        if not isinstance(item, dict):
            continue
        analyte_id = item.get("analyte_id")
        if analyte_id not in batch_ids or analyte_id in out:
            continue
        action = item.get("action")
        confidence = item.get("confidence")
        if action not in prompt.ACTIONS or confidence not in prompt.CONFIDENCES:
            continue
        reason = item.get("reason_code")
        reason = reason if reason in prompt.REASON_CODES else None
        target_id = item.get("target_id") or None
        name = " ".join(str(item.get("display_name") or "").split())[:200] or None
        if action == "merge_into":
            name = None
            if target_id not in targets or target_id == analyte_id:
                action, target_id, reason = "unsure", None, "invalid_target"
        else:
            target_id = None
            if action == "unsure":
                name = None
        out[analyte_id] = Proposal(analyte_id, action, target_id, name, confidence, reason)
    return out


def ask(
    client, model: str, catalogue_text: str, batch_ids: Iterable[int], text: str, targets
) -> BatchAnswer:
    """One batch. Transient API trouble raises ``JobError`` (retry later), a
    bad key or model ``PermanentJobError``; a refused, truncated or malformed
    answer is a ``BatchAnswer.error`` (the batch stays untriaged)."""
    import anthropic

    answer = BatchAnswer()
    try:
        response = client.beta.messages.create(**request_params(model, catalogue_text, text))
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
    proposals = parse_proposals(data, set(batch_ids), targets)
    if proposals is None:
        answer.error = "invalid_json"
        return answer
    answer.proposals = proposals
    return answer


# ── applying ────────────────────────────────────────────────────────────────


def units_match(db: DbSession, source: Analyte, target: Analyte) -> bool | None:
    """Every unit seen on the source is the target's default unit or one seen
    on the target. ``None`` (unknown) when the source has no unit at all:
    nothing shows the two measure the same thing, so such a merge is never
    auto-applied by ``--apply`` and stays a proposal for an admin (#113)."""
    seen = units_seen(db, [source.id, target.id])
    source_units = {normalize_unit(u) for u in _units(source, seen[source.id])}
    if not source_units:
        return None
    target_units = {normalize_unit(u) for u in _units(target, seen[target.id])}
    return source_units <= target_units


def auto_applicable(proposal: Proposal, source: Analyte, target: Analyte | None, match) -> bool:
    if proposal.confidence != "high":
        return False
    if proposal.action == "keep_as_new":
        return True
    return (
        proposal.action == "merge_into"
        and target is not None
        and target.specimen == source.specimen
        and match is True
    )


def _set_triage(analyte: Analyte, **values) -> None:
    analyte.triage = {**(analyte.triage or {}), **values}


def apply_proposal(
    db: DbSession, analyte: Analyte, *, user_id: int | None, status: str = STATUS_ACCEPTED
) -> Analyte:
    """Apply the analyte's pending proposal through the review services
    (caller commits; raises ``analytes.AnalyteError``). Returns the merge
    target, or the confirmed analyte."""
    triage = analyte.triage or {}
    if not analyte.needs_review or triage.get("status") != STATUS_PENDING:
        raise analyte_svc.AnalyteError(409, "Esta propuesta ya no está pendiente.")
    action = triage.get("action")
    if action == "merge_into":
        return analyte_svc.merge_analytes(
            db, target_id=triage.get("target_id"), source_id=analyte.id, user_id=user_id
        )
    if action == "keep_as_new":
        _set_triage(analyte, status=status, error=None)
        changes: dict[str, Any] = {"needs_review": False}
        name = triage.get("display_name")
        if name and name != analyte.canonical_name:
            changes["canonical_name"] = name
        return analyte_svc.update_analyte(db, analyte, changes, user_id=user_id)
    raise analyte_svc.AnalyteError(409, "La IA no hizo una propuesta para este examen.")


def reject_proposal(db: DbSession, analyte: Analyte, *, user_id: int | None) -> Analyte:
    from app.services import audit

    triage = analyte.triage or {}
    if triage.get("status") != STATUS_PENDING:
        raise analyte_svc.AnalyteError(409, "Esta propuesta ya no está pendiente.")
    before = audit.snapshot(analyte)
    _set_triage(analyte, status=STATUS_REJECTED)
    analyte.updated_by = user_id
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity=analyte_svc.ENTITY,
        entity_id=analyte.id,
        before=before,
        after=audit.snapshot(analyte),
    )
    return analyte


def pending_proposals(db: DbSession) -> list[Analyte]:
    """Analytes still under review with an actionable pending proposal."""
    return [
        a
        for a in db.scalars(
            select(Analyte)
            .where(Analyte.needs_review.is_(True), Analyte.triage.is_not(None))
            .order_by(Analyte.id)
        )
        if a.triage.get("status") == STATUS_PENDING and a.triage.get("action") != "unsure"
    ]


def accept_all(db: DbSession, *, user_id: int | None) -> dict[str, int]:
    """«Aceptar todas las propuestas»: each in its own savepoint; a refused
    one (specimen mismatch, name taken, target gone) stays pending with an
    ``error`` code. Caller commits."""
    counts = {"accepted": 0, "failed": 0}
    for analyte_id in [a.id for a in pending_proposals(db)]:
        analyte = db.get(Analyte, analyte_id)
        if analyte is None:  # merged away meanwhile
            continue
        try:
            with db.begin_nested():
                apply_proposal(db, analyte, user_id=user_id)
            counts["accepted"] += 1
        except analyte_svc.AnalyteError as exc:
            analyte = db.get(Analyte, analyte_id)
            if analyte is not None:
                _set_triage(analyte, error=f"refused_{exc.status}")
                db.flush()
            counts["failed"] += 1
    return counts


def triage_job_active(db: DbSession) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.kind == JOB_KIND, Job.status.in_(("queued", "running")))
        .order_by(Job.id)
        .limit(1)
    )


# ── the run ─────────────────────────────────────────────────────────────────


@dataclass
class TriageStats:
    candidates: int = 0
    batches: int = 0
    proposals: Counter = field(default_factory=Counter)  # by action
    high_confidence: int = 0
    auto_applied: Counter = field(default_factory=Counter)  # merged / kept
    apply_failed: int = 0
    skipped: int = 0  # reviewed meanwhile
    errors: Counter = field(default_factory=Counter)
    usage: Usage = field(default_factory=Usage)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "batches": self.batches,
            "proposals": dict(self.proposals),
            "high_confidence": self.high_confidence,
            "auto_applied": dict(self.auto_applied),
            "apply_failed": self.apply_failed,
            "skipped": self.skipped,
            "errors": dict(self.errors),
            "usage": self.usage.as_dict(),
        }


def run_triage(
    db: DbSession,
    settings: Settings,
    *,
    client=None,
    apply: bool = False,
    dry_run: bool = False,
    user_id: int | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> TriageStats:
    """Triage every pending analyte, batch by batch (see the module docstring).
    ``dry_run`` asks Claude (so the counts are real) but saves nothing."""
    if client is None:
        if not settings.anthropic_api_key:
            raise PermanentJobError("Falta la clave de Claude (ANTHROPIC_API_KEY).")
        client = make_client(settings.anthropic_api_key)
    model = settings.extraction_model
    stats = TriageStats(candidates=pending_count(db))
    total_batches = max(1, -(-stats.candidates // BATCH_SIZE))
    after_id = 0
    while True:
        batch = list(db.scalars(pending_query(after_id).limit(BATCH_SIZE)))
        if not batch:
            break
        after_id = batch[-1].id
        targets_rows, cat_text = catalogue(db)
        targets = {a.id: a for a in targets_rows}
        text = batch_text(db, batch)
        batch_ids = [a.id for a in batch]
        if progress is not None:
            progress(
                int(100 * stats.batches / total_batches),
                f"Revisando exámenes con IA ({stats.batches + 1} de {total_batches})",
            )
        db.rollback()  # no transaction (or connection) held across the call
        answer = ask(client, model, cat_text, batch_ids, text, targets)
        stats.batches += 1
        if answer.usage.requests:  # a 400/413 billed nothing
            stats.usage.add(answer.usage)  # same attribute names as an API usage object
        logger.info(
            "analyte triage batch: analytes=%d proposals=%d error=%s input_tokens=%d "
            "output_tokens=%d cache_read=%d",
            len(batch_ids),
            len(answer.proposals),
            answer.error,
            answer.usage.input_tokens,
            answer.usage.output_tokens,
            answer.usage.cache_read_input_tokens,
        )
        if answer.error:
            stats.errors[answer.error] += 1
            continue
        stats.errors.update({"missing": len(batch_ids) - len(answer.proposals)})
        _store_batch(db, settings, answer, stats, apply=apply, dry_run=dry_run, user_id=user_id)
    stats.errors = +stats.errors
    return stats


def _store_batch(db, settings, answer, stats, *, apply, dry_run, user_id) -> None:
    for analyte_id, proposal in sorted(answer.proposals.items()):
        analyte = db.get(Analyte, analyte_id, with_for_update=not dry_run)
        if analyte is None or not analyte.needs_review or analyte.triage is not None:
            stats.skipped += 1
            continue
        stats.proposals[proposal.action] += 1
        stats.high_confidence += proposal.confidence == "high"
        target = db.get(Analyte, proposal.target_id) if proposal.target_id else None
        match = units_match(db, analyte, target) if target is not None else None
        triage = {
            "version": SPEC.version,
            "model": settings.extraction_model,
            "action": proposal.action,
            "target_id": proposal.target_id,
            "display_name": proposal.display_name,
            "confidence": proposal.confidence,
            "reason_code": proposal.reason_code,
            "units_match": match,
            "status": STATUS_PENDING,
            "error": None,
        }
        auto = apply and auto_applicable(proposal, analyte, target, match)
        if dry_run:
            if auto:
                stats.auto_applied["merged" if target else "kept"] += 1
            continue
        analyte.triage = triage
        db.flush()
        if not auto:
            continue
        try:
            with db.begin_nested():
                apply_proposal(db, analyte, user_id=user_id, status=STATUS_APPLIED)
            stats.auto_applied["merged" if target else "kept"] += 1
        except analyte_svc.AnalyteError as exc:
            analyte = db.get(Analyte, analyte_id)
            if analyte is not None:
                _set_triage(analyte, error=f"refused_{exc.status}")
                db.flush()
            stats.apply_failed += 1
    if dry_run:
        db.rollback()
    else:
        db.commit()
