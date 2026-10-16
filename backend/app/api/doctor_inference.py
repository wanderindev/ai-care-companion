"""«Completar médicos con IA» (ticket #125), admins only.

- ``POST /doctor-inference/estimate`` ``{patient_id, date_from?, date_to?}``
  — candidate entries, calls and a rough cost (no Claude call, nothing
  written);
- ``POST /doctor-inference`` (same body) — queue the ``infer_doctors`` job
  (202 ``{job_id}``; progress via ``GET /jobs/{id}``). 409 while one is
  queued/running or when no entry is left; 503 without a Claude key.

The job only creates suggestions («Pendientes»); rules in
``app.services.doctor_inference``.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_admin
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.user import User
from app.schemas.doctor_inference import (
    DoctorInferenceEstimate,
    DoctorInferenceIn,
    DoctorInferenceStarted,
)
from app.services import audit, jobs
from app.services import doctor_inference as svc

router = APIRouter(prefix="/doctor-inference", tags=["doctors"])


@router.post("/estimate", response_model=DoctorInferenceEstimate)
def estimate(
    body: DoctorInferenceIn,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user: User = Depends(require_admin),
) -> DoctorInferenceEstimate:
    try:
        est = svc.estimate(db, settings, body.patient_id, body.date_from, body.date_to)
    except svc.InferenceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    job = svc.active_job(db)
    return DoctorInferenceEstimate(
        candidates=est.candidates,
        batches=est.batches,
        model=est.model,
        cost_usd=est.cost_usd,
        running_job_id=job.id if job is not None else None,
    )


@router.post("", response_model=DoctorInferenceStarted, status_code=status.HTTP_202_ACCEPTED)
def start(
    body: DoctorInferenceIn,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_admin),
) -> DoctorInferenceStarted:
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="La IA no está configurada.")
    try:
        svc.check_params(db, body.patient_id, body.date_from, body.date_to)
    except svc.InferenceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    if svc.active_job(db) is not None:
        raise HTTPException(status_code=409, detail="Ya hay una búsqueda de médicos en curso.")
    if not svc.candidates(db, body.patient_id, body.date_from, body.date_to):
        raise HTTPException(status_code=409, detail="No hay entradas sin médico para revisar.")
    job = jobs.enqueue(
        db,
        svc.JOB_KIND,
        svc.job_payload(body.patient_id, body.date_from, body.date_to, user.id),
        message="Búsqueda de médicos en cola",
    )
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_CREATE,
        entity="job",
        entity_id=job.id,
        after={"kind": svc.JOB_KIND, "patient_id": body.patient_id},
    )
    db.commit()
    return DoctorInferenceStarted(job_id=job.id)
