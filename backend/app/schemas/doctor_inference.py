"""«Completar médicos con IA» (ticket #125, ``app.services.doctor_inference``)."""

from datetime import date

from pydantic import BaseModel, Field


class DoctorInferenceIn(BaseModel):
    patient_id: int = Field(gt=0)
    date_from: date | None = None
    date_to: date | None = None


class DoctorInferenceEstimate(BaseModel):
    """Entries a run would send, in how many calls, and a rough cost (USD;
    null for a model without known prices). ``running_job_id``: a run in
    progress (only one at a time)."""

    candidates: int
    batches: int
    model: str
    cost_usd: float | None
    running_job_id: int | None = None


class DoctorInferenceStarted(BaseModel):
    job_id: int
