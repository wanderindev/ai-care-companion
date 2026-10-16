"""Appointments API (DESIGN §5.3, §6.3, §7): list with filters, CRUD, and
"Marcar como realizada" (``POST /appointments/{id}/complete``).

Any signed-in member can create, edit, cancel (``PATCH status=cancelada``)
and delete (a hard delete, for mistakes) appointments; every write is audited.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.api.log_entries import ATTACHMENT_CONFLICT, _detail, _next_appointment
from app.core.database import get_db
from app.models.appointment import STATUS_DONE, STATUS_SCHEDULED, Appointment
from app.models.user import User
from app.schemas.appointments import (
    AppointmentCreate,
    AppointmentOut,
    AppointmentPage,
    AppointmentUpdate,
    SortOrder,
    Status,
)
from app.schemas.log_entries import LogEntryCreate, LogEntryOut
from app.services import appointments as svc
from app.services import log_entries as entries_svc
from app.services.attachments import AttachmentLinkError

router = APIRouter(prefix="/appointments", tags=["appointments"])

NOT_FOUND = "Cita no encontrada."
NOT_SCHEDULED = {
    STATUS_DONE: "Esta cita ya se registró como realizada.",
    "cancelada": "Esta cita está cancelada; vuelve a programarla antes de registrarla.",
}


def _get_or_404(db: DbSession, appointment_id: int, *, lock: bool = False) -> Appointment:
    appt = db.get(Appointment, appointment_id, with_for_update=lock)
    if appt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return appt


def _bad_request(db: DbSession, exc: Exception) -> HTTPException:
    db.rollback()
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=AppointmentPage)
def list_appointments(
    patient_id: list[int] | None = Query(
        default=None, description="One or more patients (repeat the parameter for both)."
    ),
    doctor_id: list[int] | None = Query(default=None, description="One or more doctors."),
    status_: list[Status] | None = Query(
        default=None, alias="status", description="One or more statuses."
    ),
    date_from: date | None = Query(default=None, description="Inclusive, Panama day."),
    date_to: date | None = Query(default=None, description="Inclusive, Panama day."),
    order: SortOrder = Query(default="asc", description="By start time."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> AppointmentPage:
    """Appointments ordered by ``starts_at`` (soonest first by default), with
    the total count for paging."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="El rango de fechas no es válido."
        )
    conds = svc.filter_conditions(
        patient_ids=patient_id,
        doctor_ids=doctor_id,
        statuses=status_,
        date_from=date_from,
        date_to=date_to,
    )
    items, total = svc.list_appointments(db, conds, order=order, limit=limit, offset=offset)
    return AppointmentPage(
        items=[AppointmentOut.model_validate(a) for a in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
def create_appointment(
    payload: AppointmentCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> AppointmentOut:
    """A new ``programada`` appointment."""
    try:
        appt = svc.create_appointment(db, payload.model_dump(), user_id=user.id)
    except svc.AppointmentError as exc:
        raise _bad_request(db, exc) from None
    db.commit()
    db.refresh(appt)
    return AppointmentOut.model_validate(appt)


@router.get("/{appointment_id}", response_model=AppointmentOut)
def get_appointment(
    appointment_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> AppointmentOut:
    return AppointmentOut.model_validate(_get_or_404(db, appointment_id))


@router.patch("/{appointment_id}", response_model=AppointmentOut)
def patch_appointment(
    appointment_id: int,
    payload: AppointmentUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> AppointmentOut:
    """Change the fields present in the body: ``{"status": "cancelada"}``
    cancels; ``{"status": "realizada", "result_entry_id": …}`` records it."""
    appt = _get_or_404(db, appointment_id)
    changes = payload.model_dump(include=payload.model_fields_set)
    try:
        svc.update_appointment(db, appt, changes, user_id=user.id)
    except svc.AppointmentError as exc:
        raise _bad_request(db, exc) from None
    db.commit()
    db.refresh(appt)
    return AppointmentOut.model_validate(appt)


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_appointment(
    appointment_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    """Delete an appointment entered by mistake (to call one off, set
    ``status`` to ``cancelada``). The audit log keeps the full row."""
    svc.delete_appointment(db, _get_or_404(db, appointment_id), user_id=user.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{appointment_id}/complete",
    response_model=LogEntryOut,
    status_code=status.HTTP_201_CREATED,
)
def complete_appointment(
    appointment_id: int,
    payload: LogEntryCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> LogEntryOut:
    """ "Marcar como realizada": create the visit's bitácora entry (same body
    as ``POST /log-entries``) and set the appointment to ``realizada`` with
    ``result_entry_id`` pointing at it, in one transaction (both audited).
    Only a ``programada`` appointment can be completed (409 otherwise); the
    entry must be the appointment's patient's (400). Returns the new entry."""
    appt = _get_or_404(db, appointment_id, lock=True)
    if appt.status != STATUS_SCHEDULED:
        detail = NOT_SCHEDULED.get(appt.status, "Esta cita no está programada.")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    data = payload.model_dump(exclude={"attachment_ids", "next_appointment"})
    try:
        entry = entries_svc.create_entry(
            db,
            data,
            user_id=user.id,
            attachment_ids=payload.attachment_ids,
            next_appointment=_next_appointment(payload.next_appointment),
        )
        svc.update_appointment(
            db,
            appt,
            {"status": STATUS_DONE, "result_entry_id": entry.id},
            user_id=user.id,
        )
    except (entries_svc.LogEntryError, svc.AppointmentError) as exc:
        raise _bad_request(db, exc) from None
    except AttachmentLinkError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ATTACHMENT_CONFLICT
        ) from None
    db.commit()
    db.refresh(entry)
    return _detail(db, entry)
