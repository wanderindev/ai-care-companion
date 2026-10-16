"""Backfill analysis runs (DESIGN §5.6, §6.7 step 6; ticket #40).

One row per backfill analysis an admin started: the stored WhatsApp messages
of a date range (Panamá days, inclusive), whatever import stored them, go
through the daily-analysis pipeline with the backfill prompt and become
suggestions for one patient (``patient_id``, the focus). ``cross_check``:
the patient's bitácora is already complete, so only events missing from it
are proposed.

- ``status``: ``queued`` → ``waiting`` (for transcripts/classifications) →
  ``running`` → ``done``; ``stopped`` (by an admin, or ``stop_reason =
  cost_limit`` when ``stats.cost_usd`` reached ``max_cost_usd``) can be
  resumed. A job that failed for good shows as ``failed`` in the API (the
  row keeps the last status, like imports).
- ``stats``: the pipeline's analysis stats (``daily_analysis.pipeline.
  empty_stats``) plus ``days_total`` (days with messages to analyze) and
  ``days_already_done`` (days of the range an earlier run with the same
  patient and cross-check already analyzed: skipped unless ``reanalyze``).
- ``job_id``: the latest ``analyze_backfill`` job (continuations replace it).

Suggestions it produced carry ``suggestions.analysis_run_id``.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.job import Job
from app.models.mixins import TimestampMixin
from app.models.user import User

RUN_KINDS = ("backfill",)
RUN_STATUSES = ("queued", "waiting", "running", "done", "stopped")
ACTIVE_STATUSES = ("queued", "waiting", "running")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class AnalysisRun(TimestampMixin, Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        CheckConstraint(_in("kind", RUN_KINDS), name="ck_analysis_runs_kind"),
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_analysis_runs_status"),
        CheckConstraint("date_from <= date_to", name="ck_analysis_runs_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="backfill")
    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    cross_check: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    reanalyze: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    stop_reason: Mapped[str | None] = mapped_column(String(20))
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL")
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    creator: Mapped[User | None] = relationship(lazy="selectin", viewonly=True)
    job: Mapped[Job | None] = relationship(lazy="selectin", viewonly=True)
