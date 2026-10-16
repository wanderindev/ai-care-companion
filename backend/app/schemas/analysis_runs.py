from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class AnalysisRunEstimateIn(BaseModel):
    patient_id: int
    date_from: date
    date_to: date
    cross_check: bool = False
    # Analyze again days an earlier run with the same patient/cross-check did.
    reanalyze: bool = False


class AnalysisRunCreate(AnalysisRunEstimateIn):
    # Default: BACKFILL_MAX_COST_USD.
    max_cost_usd: float | None = None


class AnalysisRunResume(BaseModel):
    max_cost_usd: float | None = None


class AnalysisEstimateOut(BaseModel):
    days_with_messages: int
    days_already_done: int
    days_to_analyze: int
    # Days with something to read (the rest are skipped without a call).
    days_with_content: int
    messages: int
    # Audio/photos/PDFs without a transcript/description yet, and those whose
    # job is still queued or running (worth waiting for).
    media_unresolved: int
    media_in_progress: int
    calls: int
    jobs: int
    cost_usd: float
    model: str
    max_cost_usd: float


class AnalysisRunOut(BaseModel):
    id: int
    kind: str
    patient_id: int
    date_from: date
    date_to: date
    cross_check: bool
    reanalyze: bool
    # queued / waiting / running / done / stopped / failed (the job gave up).
    status: str
    stop_reason: str | None
    max_cost_usd: float
    stats: dict[str, Any]
    job_id: int | None
    pending_suggestions: int
    created_by_name: str | None
    created_at: datetime
    finished_at: datetime | None


class RejectPendingOut(BaseModel):
    rejected: int
