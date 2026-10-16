"""«Pendientes» API (DESIGN §5.6, §6.7 step 5, §7): list suggestions with
their source messages, the pending count for the header badge, a dry run for
medication changes, and approve / reject.

Any signed-in member decides (DESIGN §2: everyone can edit); every decision
and every record it writes is audited under that member. Rules live in
``app.services.suggestions``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.database import get_db
from app.models.suggestion import Suggestion
from app.models.user import User
from app.schemas.meds import MedAvoidWarning
from app.schemas.suggestions import (
    MedChangePreview,
    SourceMessageOut,
    Status,
    SuggestionApprove,
    SuggestionCount,
    SuggestionOut,
    SuggestionPage,
    SuggestionPreviewIn,
    SuggestionReject,
)
from app.services import suggestions as svc

router = APIRouter(prefix="/suggestions", tags=["suggestions"])


def _error(db: DbSession, exc: svc.SuggestionError) -> JSONResponse:
    db.rollback()
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), **exc.extra})


def _serialize(db: DbSession, rows: list[Suggestion]) -> list[SuggestionOut]:
    message_ids = sorted({i for row in rows for i in row.source_message_ids})
    messages = svc.source_messages(db, message_ids)
    user_ids = {u for row in rows for u in (row.decided_by, row.created_by) if u}
    names = (
        dict(db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids))).all())
        if user_ids
        else {}
    )
    out = []
    for row in rows:
        sources = []
        for message_id in row.source_message_ids:
            if message_id not in messages:
                continue
            message, attachment = messages[message_id]
            sources.append(
                SourceMessageOut(
                    id=message.id,
                    sent_at=message.sent_at,
                    sender=message.sender,
                    text=message.text,
                    forwarded=message.forwarded,
                    media_kind=message.media_kind,
                    attachment_id=message.attachment_id,
                    mime=attachment.mime if attachment else None,
                    filename=attachment.filename if attachment else None,
                    transcript=message.transcript,
                    classification=message.classification,
                )
            )
        sources.sort(key=lambda m: (m.sent_at, m.id))
        out.append(
            SuggestionOut(
                id=row.id,
                kind=row.kind,
                source=row.source,
                patient_id=row.patient_id,
                payload=row.payload,
                rationale=row.rationale,
                explicit_request=svc.is_explicit(row),
                source_message_ids=list(row.source_message_ids),
                import_id=row.import_id,
                analysis_run_id=row.analysis_run_id,
                chat_session_id=row.chat_session_id,
                created_by=row.created_by,
                created_by_name=names.get(row.created_by),
                status=row.status,
                decided_by=row.decided_by,
                decided_by_name=names.get(row.decided_by),
                decided_at=row.decided_at,
                decision_note=row.decision_note,
                result_ref=row.result_ref,
                created_at=row.created_at,
                source_messages=sources,
                context=svc.context_for(db, row) if row.status == "pending" else {},
            )
        )
    return out


@router.get("", response_model=SuggestionPage)
def list_suggestions(
    status_: list[Status] | None = Query(
        default=None, alias="status", description="Default: pending only."
    ),
    patient_id: list[int] | None = Query(
        default=None,
        description="One or more patients; suggestions without a patient (new doctors) "
        "are always included.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> SuggestionPage:
    """Suggestions, newest first, with their source messages (text, sender,
    media kind and attachment id, transcript, classification) and the
    per-kind ``context`` (pending ones only). The pending-only list (the
    default) shows answers to «Bot: …» messages (``explicit_request``) first."""
    statuses = status_ or ["pending"]
    conds = svc.filter_conditions(statuses=statuses, patient_ids=patient_id)
    rows, total = svc.list_suggestions(
        db, conds, limit=limit, offset=offset, explicit_first=set(statuses) == {"pending"}
    )
    return SuggestionPage(items=_serialize(db, rows), total=total, limit=limit, offset=offset)


@router.get("/count", response_model=SuggestionCount)
def count_suggestions(
    patient_id: list[int] | None = Query(default=None),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> SuggestionCount:
    """Pending suggestions, for the header badge."""
    return SuggestionCount(pending=svc.count_pending(db, patient_id))


@router.post(
    "/{suggestion_id}/preview",
    response_model=MedChangePreview,
    responses={400: {}, 404: {}, 422: {}},
)
def preview_suggestion(
    suggestion_id: int,
    body: SuggestionPreviewIn | None = None,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    """``med_change`` only: the diff, auto summary and EVITAR warnings of the
    full-list save approving it would make now (nothing is saved). ``payload``
    previews an edited proposal."""
    row = db.get(Suggestion, suggestion_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=svc.NOT_FOUND)
    try:
        result = svc.preview_med_change(db, row, body.payload if body else None)
    except svc.SuggestionError as exc:
        return _error(db, exc)
    return MedChangePreview(
        **{**result, "warnings": [MedAvoidWarning(**w) for w in result["warnings"]]}
    )


@router.post(
    "/{suggestion_id}/approve",
    response_model=SuggestionOut,
    responses={400: {}, 404: {}, 409: {}, 422: {}},
)
def approve_suggestion(
    suggestion_id: int,
    body: SuggestionApprove | None = None,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Apply the suggestion (as proposed, or with ``payload`` → ``edited``)
    through the normal write path and mark it decided, atomically. 409 when
    it was already decided (or, for medications, the list changed since the
    preview / a doctor with that name exists — ``doctor`` in the body names
    it); 422 with the reason when the change does not pass validation (the
    suggestion stays pending)."""
    body = body or SuggestionApprove()
    try:
        row = svc.approve(
            db,
            suggestion_id,
            user_id=user.id,
            payload=body.payload,
            expected_version=body.expected_version,
            change_summary=body.change_summary,
            doctor_id=body.doctor_id,
        )
    except svc.SuggestionError as exc:
        return _error(db, exc)
    db.commit()
    db.refresh(row)
    return _serialize(db, [row])[0]


@router.post(
    "/{suggestion_id}/reject",
    response_model=SuggestionOut,
    responses={404: {}, 409: {}},
)
def reject_suggestion(
    suggestion_id: int,
    body: SuggestionReject | None = None,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Discard the suggestion (optional ``reason``); nothing else changes."""
    try:
        row = svc.reject(db, suggestion_id, user_id=user.id, reason=body.reason if body else None)
    except svc.SuggestionError as exc:
        return _error(db, exc)
    db.commit()
    db.refresh(row)
    return _serialize(db, [row])[0]
