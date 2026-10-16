"""``GET /jobs/{id}``: progress of a background job (DESIGN §7).

Any signed-in family member may read any job: every user shares the same
records (DESIGN §2), and a job exposes only its kind, status, progress and a
generic message — never its payload.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.database import get_db
from app.models.job import Job
from app.models.user import User
from app.schemas.jobs import JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: int, db: DbSession = Depends(get_db), _user: User = Depends(require_user)
) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada.")
    return job
