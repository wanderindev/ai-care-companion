"""Bitácora API (DESIGN §6.1): list with filters/sort/search, CRUD, attachments."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_admin, require_user
from app.core.database import get_db
from app.models.attachment import Attachment
from app.models.log_entry import LogEntry
from app.models.user import User
from app.schemas.log_entries import (
    Channel,
    DoctorRef,
    EntryAttachment,
    EntryType,
    LogEntryBulkReview,
    LogEntryBulkReviewOut,
    LogEntryCreate,
    LogEntryListItem,
    LogEntryOut,
    LogEntryPage,
    LogEntryUpdate,
    NextAppointmentIn,
    NextAppointmentOut,
    ReviewStatus,
    SortField,
    SortOrder,
)
from app.services import log_entries as svc
from app.services.appointments import AppointmentError, linked_to_entry
from app.services.attachments import AttachmentLinkError

router = APIRouter(prefix="/log-entries", tags=["log-entries"])

NOT_FOUND = "Entrada no encontrada."
ATTACHMENT_CONFLICT = "Algún archivo adjunto no existe o pertenece a otro registro."


def _serialize[T: BaseModel](schema: type[T], entry: LogEntry, attachments: list[Attachment]) -> T:
    data = {
        name: getattr(entry, name)
        for name in schema.model_fields
        if name not in ("attachments", "doctor", "next_appointment")
    }
    data["doctor"] = DoctorRef.model_validate(entry.doctor) if entry.doctor else None
    data["attachments"] = [EntryAttachment.model_validate(a) for a in attachments]
    return schema.model_validate(data)


def _detail(db: DbSession, entry: LogEntry) -> LogEntryOut:
    out = _serialize(LogEntryOut, entry, svc.attachments_by_entry(db, [entry.id])[entry.id])
    appt = linked_to_entry(db, entry.id)
    if appt is not None:
        out.next_appointment = NextAppointmentOut.model_validate(appt)
    return out


def _next_appointment(value: NextAppointmentIn | None):
    """Service form of the "Próxima cita": only the keys the client sent
    (``doctor_id`` left out → the entry's doctor; text fields left out keep
    their value)."""
    if value is None:
        return None
    return value.model_dump(exclude_unset=True)


def _get_or_404(db: DbSession, entry_id: int) -> LogEntry:
    entry = db.get(LogEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return entry


@router.get("", response_model=LogEntryPage)
def list_log_entries(
    patient_id: list[int] | None = Query(
        default=None, description="One or more patients (repeat the parameter for both)."
    ),
    doctor_id: list[int] | None = Query(default=None, description="One or more doctors."),
    entry_type: list[EntryType] | None = Query(default=None, description="One or more types."),
    channel: list[Channel] | None = Query(default=None, description="One or more channels."),
    review_status: ReviewStatus | None = Query(
        default=None, description="`pending_review` for the «Por revisar» filter."
    ),
    date_from: date | None = Query(default=None, description="Inclusive."),
    date_to: date | None = Query(default=None, description="Inclusive."),
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Full-text search, accent/case-insensitive; every word must match "
        "(prefixes count: «neumo» finds «Neumonía»). Searches title, source, "
        "indications, family notes and the doctor's name/aliases.",
    ),
    sort: SortField = Query(default="date", description="date, doctor (name) or type."),
    order: SortOrder = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> LogEntryPage:
    """Entries, newest first by default, with the total count for paging.

    Items leave out ``origin_ref``; ``GET /log-entries/{id}`` includes it.
    """
    conds = svc.filter_conditions(
        db,
        patient_ids=patient_id,
        doctor_ids=doctor_id,
        entry_types=entry_type,
        channels=channel,
        review_status=review_status,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )
    entries, total = svc.list_entries(db, conds, sort=sort, order=order, limit=limit, offset=offset)
    files = svc.attachments_by_entry(db, [e.id for e in entries])
    return LogEntryPage(
        items=[_serialize(LogEntryListItem, e, files[e.id]) for e in entries],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=LogEntryOut, status_code=status.HTTP_201_CREATED)
def create_log_entry(
    payload: LogEntryCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> LogEntryOut:
    """A manual entry (``origin = manual``). ``attachment_ids`` are files
    uploaded with ``POST /attachments`` that are not linked to anything yet.
    ``next_appointment`` ("Próxima cita") also creates a ``programada``
    appointment linked to the entry."""
    data = payload.model_dump(exclude={"attachment_ids", "next_appointment"})
    try:
        entry = svc.create_entry(
            db,
            data,
            user_id=user.id,
            attachment_ids=payload.attachment_ids,
            next_appointment=_next_appointment(payload.next_appointment),
        )
    except (svc.LogEntryError, AppointmentError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except AttachmentLinkError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ATTACHMENT_CONFLICT
        ) from None
    db.commit()
    db.refresh(entry)
    return _detail(db, entry)


@router.post("/review", response_model=LogEntryBulkReviewOut)
def bulk_review_log_entries(
    payload: LogEntryBulkReview,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> LogEntryBulkReviewOut:
    """«Marcar todas como revisadas» (admins): every ``pending_review`` entry
    matching ``filter`` (the list's filters, read exactly as
    ``GET /log-entries`` reads them) or among ``ids`` becomes ``ok``, in one
    transaction with the rows locked, each change audited. Entries already
    reviewed are left alone."""
    if payload.ids is not None:
        conds = [LogEntry.id.in_(payload.ids)]
    else:
        f = payload.filter
        conds = svc.filter_conditions(
            db,
            patient_ids=f.patient_id,
            doctor_ids=f.doctor_id,
            entry_types=f.entry_type,
            channels=f.channel,
            date_from=f.date_from,
            date_to=f.date_to,
            q=f.q,
        )
    try:
        ids = svc.mark_reviewed(db, conds, user_id=user.id)
    except svc.LogEntryError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    db.commit()
    return LogEntryBulkReviewOut(updated=len(ids), ids=ids)


@router.get("/{entry_id}", response_model=LogEntryOut)
def get_log_entry(
    entry_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> LogEntryOut:
    return _detail(db, _get_or_404(db, entry_id))


@router.patch("/{entry_id}", response_model=LogEntryOut)
def patch_log_entry(
    entry_id: int,
    payload: LogEntryUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> LogEntryOut:
    """Change the fields present in the body. ``{"review_status": "ok"}`` marks
    an imported entry as reviewed. ``attachment_ids`` replaces the set of
    linked files (removed ones are unlinked, never deleted). ``next_appointment``
    creates/updates the entry's appointment (back to ``programada``); ``null``
    cancels it; a ``realizada`` one cannot be changed from here (400)."""
    entry = _get_or_404(db, entry_id)
    extra = {"attachment_ids", "next_appointment"}
    changes = payload.model_dump(include=payload.model_fields_set - extra)
    next_appt = (
        _next_appointment(payload.next_appointment)
        if "next_appointment" in payload.model_fields_set
        else svc.UNCHANGED
    )
    try:
        svc.update_entry(
            db,
            entry,
            changes,
            user_id=user.id,
            attachment_ids=payload.attachment_ids,
            next_appointment=next_appt,
        )
    except (svc.LogEntryError, AppointmentError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except AttachmentLinkError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ATTACHMENT_CONFLICT
        ) from None
    db.commit()
    db.refresh(entry)
    return _detail(db, entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_log_entry(
    entry_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    """Delete an entry. The audit log keeps the full entry; its files are
    unlinked (kept in storage), not deleted. Appointments it created or
    records survive (their link to it is cleared)."""
    entry = _get_or_404(db, entry_id)
    svc.delete_entry(db, entry, user_id=user.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
