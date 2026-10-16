"""Backfill analysis (DESIGN §6.7 step 6; ticket #40): stored WhatsApp
messages of a date range → suggestions for one patient.

Started only by an admin (``POST /analysis-runs``), never on upload: it costs
money. An :class:`AnalysisRun` names the patient (focus), the Panamá date
range and ``cross_check`` (the bitácora is complete: propose only what it
lacks). It reads **every stored message of the range**, whatever import
stored it (``pipeline.range_messages``): the operator uploads the full export
once as a ``backfill`` import (so its audio is transcribed and its photos/PDFs
classified), and messages stored earlier by another import count too.

The ``analyze_backfill`` job runs the daily pipeline (``pipeline.run_analysis``)
with the backfill prompt (``app.prompts.backfill_analysis_v2``), the run's
instructions above the record context, today's medication list and the
backfill rules (``pipeline.out_of_scope``). Per run:

- **Waits** for transcripts/classifications still in progress in the range,
  like the daily job, but up to ``MAX_WAITS`` (≈ 2 h: a full export queues
  many files) before going ahead.
- At most ``ANALYSIS_MAX_DAYS_PER_JOB`` days per job; then a continuation
  job. Progress ("Analizando día n de N") before every call.
- **Cost guard**: before each day, a run whose ``stats.cost_usd`` reached
  ``max_cost_usd`` stops (``stopped`` / ``cost_limit``); an admin can resume
  it with a higher limit. An admin can also stop it at any time (``stopped``
  / ``manual``); the job sees it before the next day.
- **No day twice**: ``stats.days_done`` (per run, under the run's advisory
  lock) and, at creation, ``days_already_done``: days of the range an earlier
  run with the same patient and cross-check already analyzed are skipped
  unless ``reanalyze``. Dedupe against records, pending **and rejected**
  suggestions covers a re-analysis.
- **Undo**: :func:`reject_pending` rejects the run's pending suggestions
  (audited, each like a «Rechazar» in Pendientes).

Logs carry ids and counts only.
"""

import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.models.analysis_run import ACTIVE_STATUSES, AnalysisRun
from app.models.patient import Patient
from app.models.suggestion import STATUS_PENDING, Suggestion
from app.models.whatsapp import WhatsappMessage
from app.prompts import backfill_analysis_v2
from app.services import audit, jobs, meds, suggestions
from app.services.appointments import to_local, today_local
from app.services.bitacora_import.extract import Usage
from app.services.daily_analysis import analyze, pipeline
from app.services.daily_analysis import jobs as daily_jobs
from app.services.daily_analysis.context import build_context

JOB_KIND = "analyze_backfill"
ENTITY = "analysis_run"
MAX_WAITS = 40
WAIT_DELAY = daily_jobs.WAIT_DELAY
COST_CEILING_USD = 1000
# Estimate: Spanish text runs ~3–4 characters per token (3 is on the safe
# side); the answer, adaptive thinking included, ~2 000 output tokens per call.
CHARS_PER_TOKEN = 3.0
OUTPUT_TOKENS_PER_CALL = 2000
REJECT_NOTE = "Rechazada en bloque al deshacer un análisis histórico."

PATIENT_NOT_FOUND = "Paciente no encontrado."
BAD_RANGE = "La fecha inicial debe ser anterior o igual a la final."
FUTURE_RANGE = "El periodo no puede terminar después de hoy."
RANGE_TOO_LONG = "El periodo es demasiado largo (máximo {days} días)."
BAD_COST = "El límite de costo debe ser mayor que 0 y como máximo {ceiling} USD."
ALREADY_RUNNING = "Ya hay un análisis histórico en curso para este paciente."
NOT_FOUND = "Análisis no encontrado."
NOT_RUNNING = "Este análisis no está en curso."
NOT_RESUMABLE = "Solo se puede reanudar un análisis detenido o fallido."
RAISE_LIMIT = "Sube el límite de costo por encima de lo ya gastado para continuar."
STILL_RUNNING = "Detén el análisis antes de rechazar sus sugerencias."

logger = logging.getLogger("app.daily_analysis")


class BackfillError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ── Run settings ─────────────────────────────────────────────────────────────


def meds_since(db: DbSession, patient_id: int) -> date | None:
    """Since when the patient's current medication list holds: its «Desde»,
    else the day it was saved; None without a list."""
    version = meds.latest_version(db, patient_id)
    if version is None:
        return None
    return version.effective_date or to_local(version.changed_at).date()


def options_for(
    db: DbSession,
    patient: Patient,
    cross_check: bool,
    settings: Settings,
    *,
    today: date | None = None,
) -> pipeline.Options:
    today = today or today_local()
    since = meds_since(db, patient.id)
    preamble = backfill_analysis_v2.run_instructions(
        patient_slug=patient.slug,
        patient_name=patient.display_name,
        cross_check=cross_check,
        today=today,
        meds_since=since,
    )
    return pipeline.Options(
        spec=backfill_analysis_v2.SPEC,
        model=settings.analysis_model,
        max_days=max(1, settings.analysis_max_days_per_job),
        backfill=True,
        patient_id=patient.id,
        cross_check=cross_check,
        preamble=preamble,
        today=today,
        meds_since=since,
    )


def effective_status(run: AnalysisRun) -> str:
    """``failed`` when the run's latest job gave up, else the stored status."""
    if run.status in ACTIVE_STATUSES and run.job is not None and run.job.status == "failed":
        return "failed"
    return run.status


def days_done_before(
    db: DbSession,
    patient_id: int,
    cross_check: bool,
    date_from: date,
    date_to: date,
    *,
    exclude_id: int | None = None,
) -> list[str]:
    """Days of the range that earlier runs with the same patient and
    cross-check already analyzed."""
    stmt = select(AnalysisRun.stats).where(
        AnalysisRun.patient_id == patient_id, AnalysisRun.cross_check.is_(cross_check)
    )
    if exclude_id is not None:
        stmt = stmt.where(AnalysisRun.id != exclude_id)
    lo, hi = date_from.isoformat(), date_to.isoformat()
    days: set[str] = set()
    for stats in db.scalars(stmt):
        for day in (stats or {}).get("days_done") or []:
            if isinstance(day, str) and lo <= day <= hi:
                days.add(day)
    return sorted(days)


def run_messages(db: DbSession, run: AnalysisRun) -> list[WhatsappMessage]:
    """The range's stored messages, minus the days already done by earlier runs."""
    skip = set((run.stats or {}).get("days_already_done") or [])
    messages = pipeline.range_messages(db, run.date_from, run.date_to)
    return [m for m in messages if pipeline.local_day(m.sent_at).isoformat() not in skip]


def check_range(date_from: date, date_to: date, settings: Settings) -> None:
    if date_from > date_to:
        raise BackfillError(BAD_RANGE)
    if date_to > today_local():
        raise BackfillError(FUTURE_RANGE)
    limit = max(1, settings.backfill_max_range_days)
    if (date_to - date_from).days + 1 > limit:
        raise BackfillError(RANGE_TOO_LONG.format(days=limit))


def _patient(db: DbSession, patient_id: int) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise BackfillError(PATIENT_NOT_FOUND, status=404)
    return patient


def _cost(value: float | None, settings: Settings) -> Decimal:
    cost = settings.backfill_max_cost_usd if value is None else value
    if not (0 < cost <= COST_CEILING_USD):
        raise BackfillError(BAD_COST.format(ceiling=COST_CEILING_USD))
    return Decimal(str(round(cost, 2)))


# ── Estimate ─────────────────────────────────────────────────────────────────


@dataclass
class Estimate:
    days_with_messages: int
    days_already_done: int
    days_to_analyze: int
    days_with_content: int
    messages: int
    media_unresolved: int
    media_in_progress: int
    calls: int
    jobs: int
    cost_usd: float
    model: str
    max_cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def estimate(
    db: DbSession,
    *,
    patient_id: int,
    date_from: date,
    date_to: date,
    cross_check: bool,
    reanalyze: bool,
    settings: Settings,
) -> Estimate:
    """What a run would read and roughly cost (no Claude call). Rendered
    messages and the real record context of each job's batch are measured;
    output is assumed at ``OUTPUT_TOKENS_PER_CALL`` per call."""
    check_range(date_from, date_to, settings)
    patient = _patient(db, patient_id)
    options = options_for(db, patient, cross_check, settings)
    messages = pipeline.range_messages(db, date_from, date_to)
    days = pipeline.group_by_day(messages)
    done = (
        set()
        if reanalyze
        else set(days_done_before(db, patient_id, cross_check, date_from, date_to))
    )
    todo = [d for d in days if d.isoformat() not in done]
    todo_messages = [m for d in todo for m in days[d]]
    static_tokens = _tokens(len(options.spec.system) + len(str(options.spec.schema)))
    usage = Usage()
    calls = relevant = 0
    batches = [todo[i : i + options.max_days] for i in range(0, len(todo), options.max_days)]
    job_count = 0
    for batch in batches:
        batch_calls = 0
        message_chars = 0
        for day in batch:
            if not pipeline.is_relevant(days[day]):
                continue
            relevant += 1
            lines = [(m.id, pipeline.render_message(m)) for m in days[day]]
            chunks = pipeline.chunk_lines(lines, pipeline.CHUNK_MAX_CHARS)
            batch_calls += len(chunks)
            message_chars += sum(len(t) + 1 for _, t in lines) + 120 * len(chunks)
        if not batch_calls:
            continue
        job_count += 1
        context = build_context(db, batch, today=options.today).text
        prefix = static_tokens + _tokens(len(options.preamble) + len(context))
        usage.cache_creation_input_tokens += prefix
        usage.cache_read_input_tokens += prefix * (batch_calls - 1)
        usage.input_tokens += _tokens(message_chars)
        usage.output_tokens += OUTPUT_TOKENS_PER_CALL * batch_calls
        calls += batch_calls
    cost = usage.cost_usd(options.model)
    return Estimate(
        days_with_messages=len(days),
        days_already_done=len(days) - len(todo),
        days_to_analyze=len(todo),
        days_with_content=relevant,
        messages=len(todo_messages),
        media_unresolved=len(pipeline.unresolved_media(db, todo_messages)),
        media_in_progress=len(pipeline.waiting_media(db, todo_messages)),
        calls=calls,
        jobs=job_count,
        cost_usd=round(cost or 0.0, 2),
        model=options.model,
        max_cost_usd=float(_cost(None, settings)),
    )


# ── Admin actions (the caller commits) ───────────────────────────────────────


def _enqueue(db: DbSession, run: AnalysisRun, *, waits: int = 0, delay=None):
    job = jobs.enqueue(
        db, JOB_KIND, {"run_id": run.id, "waits": waits}, message="En cola", delay=delay
    )
    run.job_id = job.id
    return job


def _active_run(db: DbSession, patient_id: int) -> AnalysisRun | None:
    rows = db.scalars(
        select(AnalysisRun).where(
            AnalysisRun.patient_id == patient_id, AnalysisRun.status.in_(ACTIVE_STATUSES)
        )
    )
    return next((r for r in rows if effective_status(r) in ACTIVE_STATUSES), None)


def start_run(
    db: DbSession,
    *,
    patient_id: int,
    date_from: date,
    date_to: date,
    cross_check: bool,
    reanalyze: bool,
    max_cost_usd: float | None,
    user_id: int,
    settings: Settings,
) -> AnalysisRun:
    check_range(date_from, date_to, settings)
    patient = db.get(Patient, patient_id, with_for_update=True)
    if patient is None:
        raise BackfillError(PATIENT_NOT_FOUND, status=404)
    if _active_run(db, patient_id) is not None:
        raise BackfillError(ALREADY_RUNNING, status=409)
    cost = _cost(max_cost_usd, settings)
    skip = [] if reanalyze else days_done_before(db, patient_id, cross_check, date_from, date_to)
    run = AnalysisRun(
        kind="backfill",
        patient_id=patient_id,
        date_from=date_from,
        date_to=date_to,
        cross_check=cross_check,
        reanalyze=reanalyze,
        status="queued",
        max_cost_usd=cost,
        stats={"days_already_done": skip},
        created_by=user_id,
    )
    db.add(run)
    db.flush()
    _enqueue(db, run)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity=ENTITY,
        entity_id=run.id,
        after=audit.snapshot(run),
    )
    return run


def get_run(db: DbSession, run_id: int, *, lock: bool = False) -> AnalysisRun:
    run = db.get(AnalysisRun, run_id, with_for_update=lock)
    if run is None:
        raise BackfillError(NOT_FOUND, status=404)
    return run


def stop_run(db: DbSession, run_id: int, *, user_id: int) -> AnalysisRun:
    run = get_run(db, run_id, lock=True)
    if effective_status(run) not in ACTIVE_STATUSES:
        raise BackfillError(NOT_RUNNING, status=409)
    before = audit.snapshot(run)
    run.status = "stopped"
    run.stop_reason = "manual"
    run.finished_at = datetime.now(UTC)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity=ENTITY,
        entity_id=run.id,
        before=before,
        after=audit.snapshot(run),
    )
    return run


def resume_run(
    db: DbSession, run_id: int, *, max_cost_usd: float | None, user_id: int, settings: Settings
) -> AnalysisRun:
    run = get_run(db, run_id, lock=True)
    if effective_status(run) not in ("stopped", "failed"):
        raise BackfillError(NOT_RESUMABLE, status=409)
    db.get(Patient, run.patient_id, with_for_update=True)
    other = _active_run(db, run.patient_id)
    if other is not None and other.id != run.id:
        raise BackfillError(ALREADY_RUNNING, status=409)
    cost = run.max_cost_usd if max_cost_usd is None else _cost(max_cost_usd, settings)
    spent = float((run.stats or {}).get("cost_usd") or 0)
    if spent >= float(cost):
        raise BackfillError(RAISE_LIMIT)
    before = audit.snapshot(run)
    run.status = "queued"
    run.stop_reason = None
    run.finished_at = None
    run.max_cost_usd = cost
    _enqueue(db, run)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity=ENTITY,
        entity_id=run.id,
        before=before,
        after=audit.snapshot(run),
    )
    return run


def reject_pending(db: DbSession, run_id: int, *, user_id: int) -> int:
    """Reject every pending suggestion of a run that is not running (each
    audited as a rejection by ``user_id``); returns how many."""
    run = get_run(db, run_id, lock=True)
    if effective_status(run) in ACTIVE_STATUSES:
        raise BackfillError(STILL_RUNNING, status=409)
    ids = list(
        db.scalars(
            select(Suggestion.id)
            .where(Suggestion.analysis_run_id == run.id, Suggestion.status == STATUS_PENDING)
            .order_by(Suggestion.id)
        )
    )
    for suggestion_id in ids:
        suggestions.reject(db, suggestion_id, user_id=user_id, reason=REJECT_NOTE)
    return len(ids)


def pending_counts(db: DbSession, run_ids: list[int]) -> dict[int, int]:
    if not run_ids:
        return {}
    rows = db.execute(
        select(Suggestion.analysis_run_id, func.count())
        .where(Suggestion.analysis_run_id.in_(run_ids), Suggestion.status == STATUS_PENDING)
        .group_by(Suggestion.analysis_run_id)
    )
    return {run_id: n for run_id, n in rows}


# ── Job ──────────────────────────────────────────────────────────────────────


def _int(value, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _set_status(db: DbSession, run: AnalysisRun, status: str, **values) -> None:
    """Change the status unless an admin stopped the run meanwhile."""
    db.execute(
        update(AnalysisRun)
        .where(AnalysisRun.id == run.id, AnalysisRun.status != "stopped")
        .values(status=status, **values)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    db.refresh(run)


def _should_stop(run: AnalysisRun, stats: dict[str, Any]) -> bool:
    if run.status == "stopped":
        return True
    if float(stats.get("cost_usd") or 0) >= float(run.max_cost_usd):
        run.status = "stopped"
        run.stop_reason = "cost_limit"
        run.finished_at = datetime.now(UTC)
        return True
    return False


@jobs.handler(JOB_KIND)
def run_analyze_backfill(ctx: jobs.JobContext) -> None:
    run_id = _int(ctx.payload.get("run_id"), -1)
    if run_id < 0:
        raise jobs.PermanentJobError("Tarea de análisis inválida.")
    waits = _int(ctx.payload.get("waits"))
    db = ctx.db
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise jobs.PermanentJobError(NOT_FOUND)
    if run.job_id is not None and run.job_id != ctx.job_id:
        # A resume (or a continuation) queued a newer job: one chain per run.
        ctx.progress(100, "Tarea reemplazada por otra más reciente")
        return
    if run.status in ("stopped", "done"):
        ctx.progress(100, "Análisis detenido" if run.status == "stopped" else "Análisis terminado")
        return
    patient = db.get(Patient, run.patient_id)
    settings = get_settings()
    opts = options_for(db, patient, run.cross_check, settings)
    messages = run_messages(db, run)
    stats = pipeline.get_stats(run, opts)
    stats["days_total"] = len(pipeline.group_by_day(messages))

    if waits < MAX_WAITS:
        waiting = pipeline.waiting_media(db, messages)
        if waiting:
            stats.update(waits=waits + 1)
            pipeline.save_stats(run, stats)
            _set_status(db, run, "waiting")
            _enqueue(db, run, waits=waits + 1, delay=WAIT_DELAY)
            logger.info(
                "backfill analysis: run %s waits for %s media message(s) (wait %s of %s)",
                run_id,
                len(waiting),
                waits + 1,
                MAX_WAITS,
            )
            ctx.progress(0, daily_jobs.WAITING_MESSAGE)
            return

    if not settings.anthropic_api_key:
        raise jobs.PermanentJobError(daily_jobs.NOT_CONFIGURED)
    pipeline.save_stats(run, stats)
    _set_status(db, run, "running")
    db.commit()
    client = analyze.make_client(settings.anthropic_api_key)
    try:
        remaining = pipeline.run_analysis(
            db,
            run,
            messages,
            client,
            opts,
            progress=lambda pct, msg: ctx.progress(pct, msg),
            should_stop=_should_stop,
        )
    except pipeline.AnalysisBusy:
        _enqueue(db, run, waits=MAX_WAITS, delay=WAIT_DELAY)
        logger.info("backfill analysis: run %s is being analyzed by another job", run_id)
        ctx.progress(0, daily_jobs.BUSY_MESSAGE)
        return
    except pipeline.AnalysisFailed as exc:
        logger.warning(
            "backfill analysis: run %s attempt %s failed: %s", run_id, ctx.attempt, exc.code
        )
        if exc.config:
            raise jobs.PermanentJobError(daily_jobs.CONFIG_REFUSED) from None
        if not exc.retryable:
            raise jobs.PermanentJobError(daily_jobs.REQUEST_REFUSED) from None
        raise jobs.JobError(daily_jobs.RETRY_MESSAGE) from None
    db.refresh(run)
    stats = pipeline.get_stats(run, opts)
    usage = stats["usage"]
    logger.info(
        "backfill analysis: run %s %s; days=%s skipped=%s failed=%s created=%s invalid=%s "
        "duplicate=%s out_of_scope=%s missing_media=%s requests=%s input=%s output=%s "
        "cache_write=%s cache_read=%s cost_usd=%s model=%s prompt=%s",
        run_id,
        "stopped" if run.status == "stopped" else "continues" if remaining else "done",
        stats["days"],
        stats["days_skipped"],
        stats["days_failed"],
        stats["suggestions_created"],
        stats["dropped_invalid"],
        stats["skipped_duplicate"],
        stats["skipped_out_of_scope"],
        stats["missing_media"],
        usage.get("requests"),
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_read_input_tokens"),
        stats["cost_usd"],
        opts.model,
        opts.spec.version,
    )
    if run.status == "stopped":
        ctx.progress(100, "Análisis detenido")
        return
    if remaining:
        _enqueue(db, run, waits=MAX_WAITS)
        ctx.progress(100, "Análisis parcial; continúa en otra tarea")
        return
    _set_status(db, run, "done", finished_at=datetime.now(UTC))
    ctx.progress(100, f"{stats['suggestions_created']} sugerencia(s) nueva(s)")
