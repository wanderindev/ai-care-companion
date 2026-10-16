"""Background jobs: enqueue, handler registry, and the worker's job lifecycle.

DESIGN §4 "Background jobs" / §5.8. No Redis/Celery: the ``jobs`` table is the
queue and ``python -m app.worker`` polls it.

Enqueueing (API side)::

    job = jobs.enqueue(db, "whatsapp_import", {"import_id": imp.id})
    # flushed, not committed: it commits with the caller's transaction, so a
    # job never exists without the record that asked for it (and vice versa).

Handlers (worker side) are plain functions registered by kind::

    @jobs.handler("whatsapp_import")
    def run_whatsapp_import(ctx: jobs.JobContext) -> None:
        ...
        ctx.progress(40, "Procesando 4 de 10 archivos")

and their module is listed in ``app.worker.HANDLER_MODULES`` so the worker
imports it at startup. Rules for handlers:

- ``ctx.db`` is the handler's own session. Work left uncommitted when the
  handler returns is committed together with ``status = 'done'``; on an
  exception it is rolled back. A handler may commit along the way (e.g. after
  each file of a batch), so it must be **idempotent**: a retry, or a run after
  the worker died mid-job, starts again from the top.
- ``ctx.progress(pct, message)`` writes progress in a separate short
  transaction (the UI sees it at once) and refreshes ``locked_at``. A job that
  reports nothing for ``LOCK_TIMEOUT`` is presumed dead and requeued, so long
  steps must report progress more often than that.
- Raise ``JobError("…")`` for an expected failure worth retrying with a
  message, ``PermanentJobError("…")`` to fail at once without retrying. Their
  messages are stored and shown, so keep them generic Spanish ("No se pudo
  leer el archivo") — never document text. Any other exception is retried
  and recorded by its **type name only** (its text may quote medical content).
- ``payload`` and ``message`` never carry medical text either: ids in, generic
  progress out.

Concurrency: a job is claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` and the
claim (``running``, ``locked_at``, ``attempts + 1``) is committed at once, so
the row lock is held only for the claim and several workers never take the
same job. Each process keeps to the pool budget: the worker uses at most two
connections at a time (the handler's session + a short progress update).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models.job import Job

logger = logging.getLogger("app.jobs")

# A running job whose locked_at is older than this is presumed dead (worker
# killed, container recreated by Watchtower) and requeued or failed.
LOCK_TIMEOUT = timedelta(minutes=15)
# Retry delay after the n-th failed attempt: BASE * 2**(n-1), capped.
RETRY_BASE_DELAY = timedelta(seconds=30)
RETRY_MAX_DELAY = timedelta(hours=1)
DEFAULT_MAX_ATTEMPTS = 3
MESSAGE_MAX = 255
ERROR_MAX = 300


class JobError(Exception):
    """An expected, retryable failure; its (generic, Spanish) message is stored."""


class PermanentJobError(JobError):
    """A failure that retrying cannot fix: the job fails at once."""


@dataclass
class JobContext:
    """What a handler gets: the job's identity, its session and a progress hook."""

    job_id: int
    kind: str
    payload: dict[str, Any]
    attempt: int
    db: Session
    _session_factory: sessionmaker = field(repr=False)

    def progress(self, percent: int, message: str | None = None) -> None:
        """Record progress (0–100) and an optional generic message; keeps the lock fresh."""
        values: dict[str, Any] = {
            "progress": max(0, min(100, int(percent))),
            "locked_at": func.now(),
        }
        if message is not None:
            values["message"] = message[:MESSAGE_MAX]
        with self._session_factory() as session:
            session.execute(
                update(Job).where(Job.id == self.job_id, Job.status == "running").values(**values)
            )
            session.commit()


Handler = Callable[[JobContext], None]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Register ``fn`` as the handler for jobs of ``kind`` (one handler per kind)."""

    def register(fn: Handler) -> Handler:
        existing = HANDLERS.get(kind)
        if existing is not None and existing is not fn:
            raise ValueError(f"a handler for job kind {kind!r} is already registered")
        HANDLERS[kind] = fn
        return fn

    return register


def enqueue(
    db: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    message: str | None = None,
    delay: timedelta | None = None,
) -> Job:
    """Add a queued job in the caller's transaction (flushed so ``job.id`` is set).

    ``delay`` postpones the first run (``run_after = now() + delay``), e.g. a
    job that waits for other jobs' results."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    job = Job(
        kind=kind,
        payload=payload or {},
        status="queued",
        progress=0,
        attempts=0,
        max_attempts=max_attempts,
        message=message[:MESSAGE_MAX] if message else None,
    )
    if delay is not None:
        job.run_after = func.now() + delay
    db.add(job)
    db.flush()
    return job


def retry_delay(attempt: int) -> timedelta:
    """Backoff before the next try after the ``attempt``-th failure (1-based)."""
    delay = RETRY_BASE_DELAY * (2 ** max(0, attempt - 1))
    return min(delay, RETRY_MAX_DELAY)


def describe_error(exc: BaseException) -> str:
    """What gets stored and logged for a failure: never arbitrary exception text."""
    if isinstance(exc, JobError) and str(exc):
        return f"{type(exc).__name__}: {exc}"[:ERROR_MAX]
    return type(exc).__name__[:ERROR_MAX]


def claim_next(db: Session) -> Job | None:
    """Take the oldest due queued job (``FOR UPDATE SKIP LOCKED``) and mark it running.

    Commits the claim. Returns ``None`` when nothing is due or every due job is
    being claimed by another worker right now.
    """
    job = db.scalars(
        select(Job)
        .where(Job.status == "queued", Job.run_after <= func.now())
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.locked_at = func.now()
    job.attempts = Job.attempts + 1
    job.error = None
    db.commit()
    db.refresh(job)
    return job


def recover_stuck_jobs(db: Session, lock_timeout: timedelta = LOCK_TIMEOUT) -> int:
    """Requeue (or fail, when out of attempts) running jobs with a stale lock."""
    stuck = list(
        db.scalars(
            select(Job)
            .where(Job.status == "running", Job.locked_at < func.now() - lock_timeout)
            .with_for_update(skip_locked=True)
        )
    )
    for job in stuck:
        job.locked_at = None
        job.error = "JobTimeout"
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            logger.warning("job %s (%s) timed out and failed", job.id, job.kind)
        else:
            job.status = "queued"
            job.run_after = func.now()
            logger.warning("job %s (%s) timed out; requeued", job.id, job.kind)
    db.commit()
    return len(stuck)


def _record_failure(db: Session, job_id: int, kind: str, exc: Exception) -> None:
    job = db.get(Job, job_id, with_for_update=True)
    if job is None:  # deleted while running
        db.commit()
        return
    error = describe_error(exc)
    job.locked_at = None
    job.error = error
    if isinstance(exc, PermanentJobError) or job.attempts >= job.max_attempts:
        job.status = "failed"
        logger.warning(
            "job %s (%s) failed after %s attempt(s): %s", job_id, kind, job.attempts, error
        )
    else:
        delay = retry_delay(job.attempts)
        job.status = "queued"
        job.run_after = func.now() + delay
        logger.warning(
            "job %s (%s) attempt %s failed: %s; retrying in %ss",
            job_id,
            kind,
            job.attempts,
            error,
            int(delay.total_seconds()),
        )
    db.commit()


def run_next(session_factory: sessionmaker) -> bool:
    """Claim and run one due job. Returns ``False`` when there was nothing to do."""
    with session_factory() as db:
        job = claim_next(db)
        if job is None:
            return False
        job_id, kind, attempt = job.id, job.kind, job.attempts
        ctx = JobContext(
            job_id=job_id,
            kind=kind,
            payload=dict(job.payload or {}),
            attempt=attempt,
            db=db,
            _session_factory=session_factory,
        )
        logger.info("job %s (%s) started, attempt %s", job_id, kind, attempt)
        try:
            fn = HANDLERS.get(kind)
            if fn is None:
                raise PermanentJobError("Tipo de tarea desconocido.")
            fn(ctx)
            db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status="done", progress=100, locked_at=None, error=None)
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            # Full tracebacks only locally: an exception's text may quote
            # document content, and production logs never carry medical text.
            if get_settings().is_local:
                logger.debug("job %s (%s) traceback", job_id, kind, exc_info=True)
            _record_failure(db, job_id, kind, exc)
            return True
        logger.info("job %s (%s) done", job_id, kind)
        return True
