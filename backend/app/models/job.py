"""Background jobs (DESIGN §4 "Background jobs", §5.8).

A row per unit of background work, polled by the worker process
(``python -m app.worker``) with ``SELECT … FOR UPDATE SKIP LOCKED``. The API
enqueues with ``app.services.jobs.enqueue`` and the UI polls ``GET /jobs/{id}``
for ``status`` / ``progress`` / ``message``.

Privacy: ``payload`` holds ids and options only (an import id, an attachment
id), never medical text; ``message`` is a short, generic Spanish progress line
shown to the family ("Procesando 3 de 10"); ``error`` is the exception type
(plus the message of a deliberate ``JobError``), never document content.

``attempts`` counts claims (incremented when the worker takes the job);
``run_after`` delays a retry (exponential backoff); ``locked_at`` is set while
running and refreshed by every progress update, so a job whose worker died is
recognised by a stale ``locked_at`` and requeued (or failed when out of
attempts).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

JOB_STATUSES = ("queued", "running", "done", "failed")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in JOB_STATUSES) + ")",
            name="ck_jobs_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_jobs_progress"),
        # The worker's poll: the oldest due queued job.
        Index("ix_jobs_queued_due", "run_after", "id", postgresql_where=text("status = 'queued'")),
        # Stuck-job recovery scans running jobs by lock age.
        Index("ix_jobs_running_locked", "locked_at", postgresql_where=text("status = 'running'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    message: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
