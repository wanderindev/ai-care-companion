from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.database import get_db
from app.models.patient import Patient
from app.models.user import User
from app.schemas.people import PatientOut, PatientUpdate
from app.services.patients import update_patient

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("", response_model=list[PatientOut])
def list_patients(
    db: DbSession = Depends(get_db), _user: User = Depends(require_user)
) -> list[Patient]:
    return list(db.scalars(select(Patient).order_by(Patient.id)))


@router.patch("/{patient_id}", response_model=PatientOut)
def patch_patient(
    patient_id: int,
    payload: PatientUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente no encontrado.")
    changes = payload.model_dump(include=payload.model_fields_set)
    return update_patient(db, patient, changes, user_id=user.id)
