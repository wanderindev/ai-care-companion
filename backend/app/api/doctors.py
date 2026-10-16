from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_admin, require_user
from app.core.database import get_db
from app.models.doctor import Doctor
from app.models.user import User
from app.schemas.people import DoctorCreate, DoctorMerge, DoctorOut, DoctorUpdate
from app.services import audit
from app.services.doctors import (
    DoctorConflict,
    MergeError,
    clean_aliases,
    find_conflict,
    match_doctors,
    merge_doctors,
)
from app.services.doctors import create_doctor as create_doctor_row

router = APIRouter(prefix="/doctors", tags=["doctors"])

NOT_FOUND = "Médico no encontrado."


def _conflict_error(existing: Doctor) -> HTTPException:
    """409 that names the doctor already using the name/alias, so the UI can say which."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Ya existe un médico con ese nombre o alias: {existing.name}.",
    )


@router.get("", response_model=list[DoctorOut])
def list_doctors(
    match: str | None = Query(default=None, max_length=200),
    include_inactive: bool = True,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> list[Doctor]:
    """All doctors by name; ``?match=`` returns the ones whose name or alias
    matches (accent/case/title-insensitive, see ``app.services.doctors``)."""
    if match is not None:
        return match_doctors(db, match, include_inactive=include_inactive)
    stmt = select(Doctor).order_by(func.lower(Doctor.name), Doctor.id)
    if not include_inactive:
        stmt = stmt.where(Doctor.is_active.is_(True))
    return list(db.scalars(stmt))


@router.post("", response_model=DoctorOut, status_code=status.HTTP_201_CREATED)
def create_doctor(
    payload: DoctorCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> Doctor:
    try:
        doctor = create_doctor_row(db, payload.model_dump(), user_id=user.id)
    except DoctorConflict as exc:
        raise _conflict_error(exc.existing) from None
    db.commit()
    db.refresh(doctor)
    return doctor


@router.patch("/{doctor_id}", response_model=DoctorOut)
def patch_doctor(
    doctor_id: int,
    payload: DoctorUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> Doctor:
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    before = audit.snapshot(doctor)
    for field, value in payload.model_dump(include=payload.model_fields_set).items():
        setattr(doctor, field, value)
    doctor.aliases = clean_aliases(doctor.name, doctor.aliases)
    with db.no_autoflush:
        existing = find_conflict(db, doctor, exclude_id=doctor.id)
    if existing is not None:
        error = _conflict_error(existing)  # read the name before the rollback expires it
        db.rollback()
        raise error
    after = audit.snapshot(doctor)
    if after != before:
        doctor.updated_by = user.id
        audit.record(
            db,
            user_id=user.id,
            action=audit.ACTION_UPDATE,
            entity="doctor",
            entity_id=doctor.id,
            before=before,
            after=after,
        )
    db.commit()
    db.refresh(doctor)
    return doctor


@router.post("/{doctor_id}/merge", response_model=DoctorOut)
def merge_doctor(
    doctor_id: int,
    payload: DoctorMerge,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> Doctor:
    """Admins only: fold the duplicate ``source_id`` into this doctor.

    Its bitácora entries (and any other row pointing at it) move here, its
    name and aliases become aliases of this doctor, empty contact fields are
    filled from it, and the duplicate is deleted. 422 for a doctor merged
    into itself, 404 when either is missing, 409 when this doctor is
    inactive. See ``app.services.doctors.merge_doctors``.
    """
    try:
        target = merge_doctors(
            db, target_id=doctor_id, source_id=payload.source_id, user_id=user.id
        )
    except MergeError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    db.commit()
    db.refresh(target)
    return target
