"""Background worker entrypoint: ``python -m app.worker``.

Polls the ``jobs`` table (DESIGN §4, §5.8; lifecycle in
``app.services.jobs``): recovers jobs whose lock went stale, then claims and
runs due jobs one at a time with ``SELECT … FOR UPDATE SKIP LOCKED``, sleeping
``POLL_INTERVAL_SECONDS`` when the queue is empty. Runs as the
``n16chat-worker`` container (same image as the backend) in both compose files.

A background thread touches a heartbeat file the prod compose healthcheck
reads, so a long job does not make the container look unhealthy. SIGTERM /
SIGINT stop the loop after the current job; a job cut short by a hard kill is
requeued by stuck-job recovery once its lock is older than
``app.services.jobs.LOCK_TIMEOUT``.

Database errors (including "relation jobs does not exist" before migration
0005 is applied) are logged by type and retried after ``ERROR_BACKOFF_SECONDS``.

Handlers: add the module that registers a kind with ``@jobs.handler("kind")``
to ``HANDLER_MODULES``.
"""

import importlib
import logging
import signal
import threading
import time
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services import jobs

# docker-compose.prod.yml's worker healthcheck reads this file's mtime.
HEARTBEAT_PATH = Path("/tmp/n16chat-worker.heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 2.0
ERROR_BACKOFF_SECONDS = 30.0
RECOVERY_INTERVAL_SECONDS = 60.0

# Modules that register job handlers (imported at startup). Later tickets
# append theirs here.
HANDLER_MODULES: tuple[str, ...] = (
    # whatsapp_import + whatsapp_media (the sync extension's files, after_media).
    "app.services.whatsapp_import.jobs",
    # Also registers the after_import hooks that queue lab_extract jobs and
    # record lab portal links (lab_link_fetch jobs).
    "app.services.labs.jobs",
    # appointment_invite: email calendar invites (DESIGN §6.3).
    "app.services.calendar_invites",
    # transcribe_audio + the after_import hook that queues it (DESIGN §6.7).
    "app.services.transcription.jobs",
    # classify_media + the after_import hook that queues it; lab reports go on
    # to lab_extract (DESIGN §6.7 step 3).
    "app.services.media_classification.jobs",
    # analyze_import + the after_import hook that queues it for `daily`
    # imports: new messages → suggestions (DESIGN §6.7 step 4).
    "app.services.daily_analysis.jobs",
    # analyze_backfill: admin-started backfill runs over a date range of
    # stored messages (DESIGN §6.7 step 6).
    "app.services.daily_analysis.backfill",
    # infer_doctors: admin-started AI proposals of a missing entry doctor (#125).
    "app.services.doctor_inference",
    # classify_substance: AI proposal for a medication name outside the LEDD
    # table, queued after a list save (#133).
    "app.services.ledd_substances",
)

logger = logging.getLogger("app.worker")


def load_handlers(modules: tuple[str, ...] = HANDLER_MODULES) -> None:
    for name in modules:
        importlib.import_module(name)


def _heartbeat(stop: threading.Event, path: Path, interval: float) -> None:
    while not stop.is_set():
        try:
            path.touch()
        except OSError:
            logger.warning("could not touch the worker heartbeat file")
        stop.wait(interval)


def run(
    stop: threading.Event,
    session_factory: sessionmaker | None = None,
    *,
    heartbeat_path: Path = HEARTBEAT_PATH,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    error_backoff: float = ERROR_BACKOFF_SECONDS,
    recovery_interval: float = RECOVERY_INTERVAL_SECONDS,
) -> None:
    """Run jobs until ``stop`` is set."""
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal

    beat = threading.Thread(
        target=_heartbeat,
        args=(stop, heartbeat_path, heartbeat_interval),
        name="worker-heartbeat",
        daemon=True,
    )
    beat.start()
    logger.info("worker started (handlers: %s)", ", ".join(sorted(jobs.HANDLERS)) or "none")

    next_recovery = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_recovery:
                with session_factory() as db:
                    jobs.recover_stuck_jobs(db)
                next_recovery = time.monotonic() + recovery_interval
            worked = jobs.run_next(session_factory)
        except Exception as exc:  # database down, table missing, …
            logger.error("worker poll failed: %s", type(exc).__name__)
            stop.wait(error_backoff)
            continue
        if not worked:
            stop.wait(poll_interval)
    beat.join(timeout=5)
    logger.info("worker stopped")


def main() -> None:
    configure_logging(get_settings().log_level)
    load_handlers()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run(stop)


if __name__ == "__main__":
    main()
