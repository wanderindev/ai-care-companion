"""Backfill analysis runs (DESIGN §6.7 step 6, §7; ticket #40), import admins only
(``require_importer``: admin + ``users.can_import``, ticket #96).

- ``POST /analysis-runs/estimate`` — what a run over ``{patient_id,
  date_from, date_to, cross_check, reanalyze}`` would read and roughly cost
  (no Claude call, nothing written);
- ``POST /analysis-runs`` — start one (+ ``max_cost_usd``; 409 while the
  patient has one in progress);
- ``GET /analysis-runs`` (latest first) and ``GET /analysis-runs/{id}``;
- ``POST /analysis-runs/{id}/stop`` / ``…/resume`` (``{max_cost_usd?}``);
- ``POST /analysis-runs/{id}/reject-pending`` — undo: reject the run's
  pending suggestions.

Rules in ``app.services.daily_analysis.backfill``.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_importer
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.analysis_run import AnalysisRun
from app.models.user import User
from app.schemas.analysis_runs import (
    AnalysisEstimateOut,
    AnalysisRunCreate,
    AnalysisRunEstimateIn,
    AnalysisRunOut,
    AnalysisRunResume,
    RejectPendingOut,
)
from app.services.daily_analysis import backfill

router = APIRouter(prefix="/analysis-runs", tags=["imports"])

LIST_LIMIT = 20


def _out(run: AnalysisRun, pending: int) -> AnalysisRunOut:
    return AnalysisRunOut(
        id=run.id,
        kind=run.kind,
        patient_id=run.patient_id,
        date_from=run.date_from,
        date_to=run.date_to,
        cross_check=run.cross_check,
        reanalyze=run.reanalyze,
        status=backfill.effective_status(run),
        stop_reason=run.stop_reason,
        max_cost_usd=float(run.max_cost_usd),
        stats=run.stats or {},
        job_id=run.job_id,
        pending_suggestions=pending,
        created_by_name=run.creator.display_name if run.creator else None,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


def _one(db: DbSession, run: AnalysisRun) -> AnalysisRunOut:
    return _out(run, backfill.pending_counts(db, [run.id]).get(run.id, 0))


def _error(db: DbSession, exc: backfill.BackfillError) -> HTTPException:
    db.rollback()
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/estimate", response_model=AnalysisEstimateOut)
def estimate_run(
    body: AnalysisRunEstimateIn,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_importer),
    settings: Settings = Depends(get_settings),
) -> AnalysisEstimateOut:
    try:
        result = backfill.estimate(
            db,
            patient_id=body.patient_id,
            date_from=body.date_from,
            date_to=body.date_to,
            cross_check=body.cross_check,
            reanalyze=body.reanalyze,
            settings=settings,
        )
    except backfill.BackfillError as exc:
        raise _error(db, exc) from None
    db.rollback()
    return AnalysisEstimateOut(**result.as_dict())


@router.get("", response_model=list[AnalysisRunOut])
def list_runs(
    db: DbSession = Depends(get_db), _user: User = Depends(require_importer)
) -> list[AnalysisRunOut]:
    rows = list(db.scalars(select(AnalysisRun).order_by(AnalysisRun.id.desc()).limit(LIST_LIMIT)))
    pending = backfill.pending_counts(db, [r.id for r in rows])
    return [_out(r, pending.get(r.id, 0)) for r in rows]


@router.post("", response_model=AnalysisRunOut, status_code=status.HTTP_201_CREATED)
def create_run(
    body: AnalysisRunCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_importer),
    settings: Settings = Depends(get_settings),
) -> AnalysisRunOut:
    try:
        run = backfill.start_run(
            db,
            patient_id=body.patient_id,
            date_from=body.date_from,
            date_to=body.date_to,
            cross_check=body.cross_check,
            reanalyze=body.reanalyze,
            max_cost_usd=body.max_cost_usd,
            user_id=user.id,
            settings=settings,
        )
    except backfill.BackfillError as exc:
        raise _error(db, exc) from None
    db.commit()
    db.refresh(run)
    return _one(db, run)


@router.get("/{run_id}", response_model=AnalysisRunOut)
def get_run(
    run_id: int, db: DbSession = Depends(get_db), _user: User = Depends(require_importer)
) -> AnalysisRunOut:
    try:
        run = backfill.get_run(db, run_id)
    except backfill.BackfillError as exc:
        raise _error(db, exc) from None
    return _one(db, run)


@router.post("/{run_id}/stop", response_model=AnalysisRunOut)
def stop_run(
    run_id: int, db: DbSession = Depends(get_db), user: User = Depends(require_importer)
) -> AnalysisRunOut:
    try:
        run = backfill.stop_run(db, run_id, user_id=user.id)
    except backfill.BackfillError as exc:
        raise _error(db, exc) from None
    db.commit()
    db.refresh(run)
    return _one(db, run)


@router.post("/{run_id}/resume", response_model=AnalysisRunOut)
def resume_run(
    run_id: int,
    body: AnalysisRunResume | None = None,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_importer),
    settings: Settings = Depends(get_settings),
) -> AnalysisRunOut:
    try:
        run = backfill.resume_run(
            db,
            run_id,
            max_cost_usd=body.max_cost_usd if body else None,
            user_id=user.id,
            settings=settings,
        )
    except backfill.BackfillError as exc:
        raise _error(db, exc) from None
    db.commit()
    db.refresh(run)
    return _one(db, run)


@router.post("/{run_id}/reject-pending", response_model=RejectPendingOut)
def reject_pending(
    run_id: int, db: DbSession = Depends(get_db), user: User = Depends(require_importer)
) -> RejectPendingOut:
    try:
        count = backfill.reject_pending(db, run_id, user_id=user.id)
    except backfill.BackfillError as exc:
        raise _error(db, exc) from None
    db.commit()
    return RejectPendingOut(rejected=count)
