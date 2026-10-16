"""Worker side of the daily analysis (listed in ``app.worker.HANDLER_MODULES``).

- ``after_import`` hook ``enqueue_analysis``: when a ``daily`` import
  finishes with new messages, queue one ``analyze_import`` job delayed by
  ``FIRST_DELAY`` (transcription and classification jobs, queued by the same
  import's hooks, usually finish by then). Payload ``{"import_id": id,
  "waits": 0}``. ``backfill`` imports are analyzed only when an admin starts
  a run (``backfill.py``); ``labs_only`` never.
- ``analyze_import`` handler:

  0. **Extension files**: while the import is ``awaiting_media`` (a sync
     extension run still uploading files, ``whatsapp_import.media_upload``)
     and less than ``MEDIA_WAIT`` after it finished, queue a copy of itself
     ``WAIT_DELAY`` later without spending a wait. Past ``MEDIA_WAIT`` it
     finalizes the run (``media_upload.finalize``: a run the extension never
     completed) and, while its ``whatsapp_media`` job is pending, waits like
     in step 1 (spending waits).
  1. **Wait** while some new audio/image/PDF message still has its
     ``transcribe_audio`` / ``classify_media`` job queued or running
     (``pipeline.waiting_media``): queue a copy of itself with ``waits + 1``,
     delayed by ``WAIT_DELAY``, and finish. Bounded: after ``MAX_WAITS``
     waits (≈ 30 min after the first delay) it goes ahead with what it has;
     the call is told how many files are missing and each suggestion of
     such a day says so in its rationale.
  2. **Analyze** up to ``ANALYSIS_MAX_DAYS_PER_JOB`` days not done yet
     (``pipeline.run_analysis``, one commit per day). Days left → a
     continuation job (``{"import_id", "waits": MAX_WAITS}``: no new wait),
     queued at once.

``whatsapp_imports.stats.analysis``::

    {"prompt_version", "model", "status": "waiting" | "running" | "done",
     "waits", "days" (analyzed with a call), "days_skipped" (nothing to read),
     "days_failed" (a call refused / truncated / invalid), "days_done": [iso],
     "suggestions_created", "dropped_invalid", "skipped_duplicate",
     "missing_media", "usage": {token counts}, "cost_usd"}

``prompt_version`` ties the import's suggestions (``suggestions.import_id``)
to the prompt module that produced them.

Concurrency: every day's transaction takes a per-import advisory lock
(``pipeline.lock_import``) and re-reads the stats; a job that cannot get it
(another copy is running) queues a copy ``WAIT_DELAY`` later and finishes.
Progress is reported before every Claude call, which keeps the job's lock
(``locked_at``) fresh during long days.

Failures: no key, or a key/model the API refuses (401/403/404) → the job
fails at once with a generic message (days already done stay done; re-run
once fixed). A 400/413/422 before any call of the job succeeded (a broken
request, e.g. the schema) also fails the job, with no day marked done.
429/5xx/network → retried with the job's backoff. A refusal, a truncated or
invalid answer, or a 400/413/422 after a successful call counts the day as
failed and goes on. Logs carry ids and counts only.
"""

import logging
from datetime import timedelta

from sqlalchemy.orm import Session as DbSession

from app.core.config import get_settings
from app.models.whatsapp import WhatsappImport
from app.prompts import daily_analysis_v2
from app.services import jobs
from app.services.daily_analysis import analyze, pipeline
from app.services.whatsapp_import import after_import, media_upload

JOB_KIND = "analyze_import"
ANALYZE_MODES = frozenset({"daily"})
FIRST_DELAY = timedelta(minutes=2)
WAIT_DELAY = timedelta(minutes=3)
MAX_WAITS = 10

NOT_CONFIGURED = "El análisis automático de mensajes no está configurado."
CONFIG_REFUSED = "No se pudo usar el servicio de análisis de mensajes."
RETRY_MESSAGE = "No se pudieron analizar los mensajes por ahora; se volverá a intentar."
WAITING_MESSAGE = "Esperando transcripciones y descripciones de archivos"
WAITING_EXTENSION_MESSAGE = "Esperando los archivos de la extensión"
BUSY_MESSAGE = "Otra tarea está analizando esta importación"
REQUEST_REFUSED = "El servicio de análisis rechazó la solicitud."

logger = logging.getLogger("app.daily_analysis")


def enqueue_analysis(db: DbSession, import_id: int, *, waits: int = 0, delay=None):
    """Queue an ``analyze_import`` job (caller commits)."""
    return jobs.enqueue(
        db, JOB_KIND, {"import_id": import_id, "waits": waits}, message="En cola", delay=delay
    )


def options() -> pipeline.Options:
    settings = get_settings()
    return pipeline.Options(
        spec=daily_analysis_v2.SPEC,
        model=settings.analysis_model,
        max_days=max(1, settings.analysis_max_days_per_job),
    )


@after_import
def enqueue_analysis_after_import(db: DbSession, imp: WhatsappImport) -> None:
    if imp.mode not in ANALYZE_MODES:
        return
    new = ((imp.stats or {}).get("messages") or {}).get("new")
    if not new:
        return
    enqueue_analysis(db, imp.id, delay=FIRST_DELAY)
    logger.info("daily analysis: import %s queued (%s new message(s))", imp.id, new)


def _save_waiting(db: DbSession, imp: WhatsappImport, opts: pipeline.Options, **fields) -> None:
    """Record ``status: waiting`` under the per-import lock (the same one
    ``media_upload.finish_media`` waits for), so neither loses the other's
    ``stats``. Without the lock (someone holds it) nothing is recorded."""
    if not pipeline.lock_import(db, imp.id):
        return
    db.refresh(imp)
    stats = pipeline.get_stats(imp, opts)
    stats.update(status="waiting", **fields)
    pipeline.save_stats(imp, stats)


def _wait_for_extension(ctx: jobs.JobContext, imp: WhatsappImport, opts, waits: int) -> bool:
    """Step 0. True when the job queued a copy of itself and must stop."""
    db = ctx.db
    if not imp.awaiting_media:
        return False
    if not media_upload.window_over(imp):
        # The extension may still upload this run's files: wait without
        # spending a wait (the transcriptions come after them).
        _save_waiting(db, imp, opts)
        enqueue_analysis(db, imp.id, waits=waits, delay=WAIT_DELAY)
        logger.info("daily analysis: import %s waits for the extension's files", imp.id)
        ctx.progress(0, WAITING_EXTENSION_MESSAGE)
        return True
    # Past the bound: finalize a run the extension never completed.
    row = db.get(WhatsappImport, imp.id, with_for_update=True, populate_existing=True)
    if media_upload.finalize(db, row) is not None:
        logger.info("daily analysis: import %s finalized the extension's files", imp.id)
    db.commit()
    if not row.awaiting_media or waits >= MAX_WAITS:
        return False
    # Its whatsapp_media job (which queues the transcriptions) is pending.
    _save_waiting(db, row, opts, waits=waits + 1)
    enqueue_analysis(db, imp.id, waits=waits + 1, delay=WAIT_DELAY)
    ctx.progress(0, WAITING_EXTENSION_MESSAGE)
    return True


def _int(value, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


@jobs.handler(JOB_KIND)
def run_analyze_import(ctx: jobs.JobContext) -> None:
    import_id = _int(ctx.payload.get("import_id"), -1)
    if import_id < 0:
        raise jobs.PermanentJobError("Tarea de análisis inválida.")
    waits = _int(ctx.payload.get("waits"))
    db = ctx.db
    imp = db.get(WhatsappImport, import_id)
    if imp is None:
        raise jobs.PermanentJobError("La importación ya no existe.")
    if imp.mode not in ANALYZE_MODES or imp.status != "done":
        raise jobs.PermanentJobError("Esta importación no se analiza automáticamente.")
    opts = options()
    if _wait_for_extension(ctx, imp, opts, waits):
        return
    stats = pipeline.get_stats(imp, opts)
    messages = pipeline.new_messages(db, import_id)

    if waits < MAX_WAITS:
        waiting = pipeline.waiting_media(db, messages)
        if waiting:
            stats.update(status="waiting", waits=waits + 1)
            pipeline.save_stats(imp, stats)
            enqueue_analysis(db, import_id, waits=waits + 1, delay=WAIT_DELAY)
            logger.info(
                "daily analysis: import %s waits for %s media message(s) (wait %s of %s)",
                import_id,
                len(waiting),
                waits + 1,
                MAX_WAITS,
            )
            ctx.progress(0, WAITING_MESSAGE)
            return

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise jobs.PermanentJobError(NOT_CONFIGURED)
    client = analyze.make_client(settings.anthropic_api_key)
    try:
        remaining = pipeline.run_analysis(
            db, imp, messages, client, opts, progress=lambda pct, msg: ctx.progress(pct, msg)
        )
    except pipeline.AnalysisBusy:
        # Another analyze_import job holds this import: look again later.
        enqueue_analysis(db, import_id, waits=MAX_WAITS, delay=WAIT_DELAY)
        logger.info("daily analysis: import %s is being analyzed by another job", import_id)
        ctx.progress(0, BUSY_MESSAGE)
        return
    except pipeline.AnalysisFailed as exc:
        logger.warning(
            "daily analysis: import %s attempt %s failed: %s", import_id, ctx.attempt, exc.code
        )
        if exc.config:
            raise jobs.PermanentJobError(CONFIG_REFUSED) from None
        if not exc.retryable:
            raise jobs.PermanentJobError(REQUEST_REFUSED) from None
        raise jobs.JobError(RETRY_MESSAGE) from None
    stats = pipeline.get_stats(imp, opts)
    usage = stats["usage"]
    logger.info(
        "daily analysis: import %s %s; days=%s skipped=%s failed=%s created=%s invalid=%s "
        "duplicate=%s missing_media=%s requests=%s input=%s output=%s cache_write=%s "
        "cache_read=%s cost_usd=%s model=%s prompt=%s",
        import_id,
        "continues" if remaining else "done",
        stats["days"],
        stats["days_skipped"],
        stats["days_failed"],
        stats["suggestions_created"],
        stats["dropped_invalid"],
        stats["skipped_duplicate"],
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
    if remaining:
        enqueue_analysis(db, import_id, waits=MAX_WAITS)
        ctx.progress(100, "Análisis parcial; continúa en otra tarea")
        return
    ctx.progress(100, f"{stats['suggestions_created']} sugerencia(s) nueva(s)")
