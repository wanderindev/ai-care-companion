"""Medication lists API (DESIGN §5.4, §6.4, §7).

Any signed-in member can read and save a patient's list; every save is a
full-list save that creates a version (rules in ``app.services.meds``). No
private patient field (CIP, birth date…) is returned in JSON; only the
printable PDF (``print.pdf``) shows the full name and CIP.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.database import get_db
from app.models.medication import SLOTS
from app.models.user import User
from app.schemas.meds import (
    MedAvoidOut,
    MedAvoidWarning,
    MedChecks,
    MedEventOut,
    MedEventsConfirm,
    MedEventsConfirmResult,
    MedEventsPage,
    MedEventUpdate,
    MedItemOut,
    MedList,
    MedListSave,
    MedSaveResult,
    MedSlotGroup,
    MedVersionDetail,
    MedVersionOut,
)
from app.services import ledd, ledd_substances, meds_checks
from app.services import meds as svc
from app.services.meds_diff import SLOT_LABELS
from app.services.pdf import PdfRenderError
from app.services.pdf import meds as meds_pdf

router = APIRouter(prefix="/patients", tags=["meds"])


def _http(exc: svc.MedsError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def _current_list(db: DbSession, patient_id: int) -> MedList:
    # Version first, then items (READ COMMITTED: each statement sees the latest
    # commit). If a save commits in between, the client gets the NEW items with
    # the OLD version_no, and its next save is a harmless 409. The other order
    # would pair OLD items with the NEW version_no: the next save would pass
    # the check and silently end the items that save had just added.
    latest = svc.latest_version(db, patient_id)
    items = svc.current_items(db, patient_id)
    ledd_items = [svc.item_snapshot(i, None) for i in items]
    return MedList(
        patient_id=patient_id,
        version_no=latest.version_no if latest else 0,
        change_summary=latest.change_summary if latest else None,
        changed_by=latest.changed_by if latest else None,
        changed_at=latest.changed_at if latest else None,
        list_note=latest.list_note if latest else None,
        effective_date=latest.effective_date if latest else None,
        slots=[
            MedSlotGroup(
                slot=slot,
                label=SLOT_LABELS[slot],
                items=[MedItemOut.model_validate(i) for i in items if i.slot == slot],
            )
            for slot in SLOTS
        ],
        avoid=[MedAvoidOut.model_validate(r) for r in svc.current_avoid(db, patient_id)],
        ledd=ledd_substances.ledd_for_items(db, ledd_items),
    )


@router.get("/{patient_id}/meds", response_model=MedList)
def get_meds(
    patient_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> MedList:
    """The current list grouped by slot (printed-sheet order), the EVITAR list
    and the current version. Items whose ``end_date`` has passed are hidden."""
    try:
        svc.get_patient(db, patient_id)
    except svc.MedsError as exc:
        raise _http(exc) from None
    return _current_list(db, patient_id)


@router.put("/{patient_id}/meds", response_model=MedSaveResult)
def put_meds(
    patient_id: int,
    payload: MedListSave,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> MedSaveResult:
    """Save the complete list → a new version with its change summary (409
    when ``expected_version`` is stale). With ``dry_run`` nothing is saved:
    the response carries the diff and the auto-generated summary for the
    confirmation step. ``warnings`` (dry run or not) lists the items that
    match the request's EVITAR list; they never block the save.
    ``effective_date`` / ``list_note`` left out keep the current values
    (``null`` clears them)."""
    items = [i.model_dump() for i in payload.items]
    avoid = [r.model_dump() for r in payload.avoid]
    warnings = [MedAvoidWarning(**w) for w in meds_checks.avoid_warnings(items, avoid)]
    # Omitted list-level fields keep the current version's value (svc.KEEP).
    sent = payload.model_fields_set
    list_fields = {
        name: getattr(payload, name) if name in sent else svc.KEEP
        for name in ("effective_date", "list_note")
    }
    try:
        outcome = svc.save_list(
            db,
            patient_id,
            items=items,
            avoid=avoid,
            expected_version=payload.expected_version,
            change_summary=payload.change_summary,
            dry_run=payload.dry_run,
            user_id=user.id,
            **list_fields,
        )
    except svc.MedsError as exc:
        raise _http(exc) from None
    if outcome.saved:
        # Unknown names get one AI classification each (ticket #133).
        ledd_substances.queue_unknown(db, items)
        db.commit()
    learned = ledd_substances.learned_map(db)
    ledd_change = ledd.change(
        ledd.compute((outcome.previous or {}).get("items") or [], learned),
        ledd.compute((outcome.snapshot or {}).get("items") or [], learned),
    )
    current = _current_list(db, patient_id)
    return MedSaveResult(
        **current.model_dump(),
        saved=outcome.saved,
        auto_summary=outcome.auto_summary,
        diff=outcome.diff,
        warnings=warnings,
        ledd_change=ledd_change,
    )


@router.get("/{patient_id}/meds/versions", response_model=list[MedVersionOut])
def list_med_versions(
    patient_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> list[MedVersionOut]:
    """Every version of the list, newest first (no snapshots)."""
    try:
        svc.get_patient(db, patient_id)
    except svc.MedsError as exc:
        raise _http(exc) from None
    learned = ledd_substances.learned_map(db)
    items = svc.version_items(db, patient_id)
    return [
        MedVersionOut(
            **v, ledd_mg=ledd.compute(items.get(v["version_no"], []), learned)["total_mg"]
        )
        for v in svc.list_versions(db, patient_id)
    ]


@router.get("/{patient_id}/meds/versions/{version_no}", response_model=MedVersionDetail)
def get_med_version(
    patient_id: int,
    version_no: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> MedVersionDetail:
    """One version: its full snapshot and the diff against the previous version."""
    try:
        svc.get_patient(db, patient_id)
        detail = svc.get_version(db, patient_id, version_no)
    except svc.MedsError as exc:
        raise _http(exc) from None
    learned = ledd_substances.learned_map(db)
    previous_items = detail.pop("previous_items")
    this = ledd.compute(detail["snapshot"].get("items") or [], learned)
    before = ledd.compute(previous_items, learned) if previous_items is not None else None
    return MedVersionDetail(**detail, ledd=this, ledd_change=ledd.change(before, this))


@router.get(
    "/{patient_id}/meds/print.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def print_med_list(
    patient_id: int,
    version: int | None = Query(default=None, ge=1),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> Response:
    """The printable list (Letter PDF, DESIGN §6.4): the current list, or
    version ``version`` from its snapshot. Sync on purpose: rendering waits
    on a WeasyPrint child process (``app.services.pdf``), so FastAPI runs
    this in its threadpool. The file name carries the patient's slug, never
    a name; nothing is cached. 503 when the renderer fails."""
    try:
        result = meds_pdf.print_meds(db, patient_id, version)
    except svc.MedsError as exc:
        raise _http(exc) from None
    except PdfRenderError:
        raise HTTPException(
            status_code=503, detail="No se pudo generar el PDF. Inténtalo de nuevo."
        ) from None
    return Response(
        content=result.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{result.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/{patient_id}/meds/events", response_model=MedEventsPage)
def list_med_events(
    patient_id: int,
    limit: int = Query(default=svc.EVENTS_PAGE, ge=1, le=svc.EVENTS_PAGE_MAX),
    cursor: str | None = Query(default=None, max_length=40),
    review_status: Literal["pending_review"] | None = Query(default=None),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> MedEventsPage:
    """The medication history reconstructed from the bitácora (``med_events``),
    newest first by ``(event_date, id)``. Each event carries its source
    entry's id and date and its ``review_status``.

    Keyset-paginated (ticket #82): at most ``limit`` events per page; pass the
    response's ``next_cursor`` as ``cursor`` for the next, older page
    (``next_cursor`` is ``null`` on the last page). A cursor is opaque; a
    malformed one is a 422.

    ``review_status=pending_review`` returns **every** pending event in one
    response (``limit`` and ``cursor`` are ignored, ``next_cursor`` is
    ``null``): the «Por revisar» box and «Confirmar todos» must never miss a
    pending event because of paging, and the pending set is bounded by review."""
    try:
        svc.get_patient(db, patient_id)
        if review_status == "pending_review":
            rows, next_cursor = svc.list_events(db, patient_id, limit=None, pending_only=True)
        else:
            before = svc.decode_cursor(cursor) if cursor else None
            rows, next_cursor = svc.list_events(db, patient_id, limit=limit, before=before)
    except svc.MedsError as exc:
        raise _http(exc) from None
    return MedEventsPage(items=[MedEventOut(**e) for e in rows], next_cursor=next_cursor)


# Review of the derived events (ticket #28): any signed-in member, like the
# bitácora's «Por revisar». Every change is audited.


@router.patch("/{patient_id}/meds/events/{event_id}", response_model=MedEventOut)
def update_med_event(
    patient_id: int,
    event_id: int,
    payload: MedEventUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> MedEventOut:
    """Edit an event (date, kind, medication, detail) and/or confirm it
    (``review_status: "ok"``). 404 when it is not this patient's."""
    try:
        row = svc.update_event(
            db, patient_id, event_id, payload.model_dump(exclude_unset=True), user_id=user.id
        )
    except svc.MedsError as exc:
        raise _http(exc) from None
    db.commit()
    return MedEventOut(**svc.event_out(db, row))


@router.delete("/{patient_id}/meds/events/{event_id}", status_code=204)
def delete_med_event(
    patient_id: int,
    event_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    """Discard an event (hard delete; the audit row keeps it, and the
    backfill never recreates it)."""
    try:
        svc.delete_event(db, patient_id, event_id, user_id=user.id)
    except svc.MedsError as exc:
        raise _http(exc) from None
    db.commit()
    return Response(status_code=204)


@router.post("/{patient_id}/meds/events/confirm", response_model=MedEventsConfirmResult)
def confirm_med_events(
    patient_id: int,
    payload: MedEventsConfirm,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> MedEventsConfirmResult:
    """«Confirmar todos»: confirm the listed events that are this patient's and
    still pending (others are ignored); returns how many were confirmed."""
    try:
        confirmed = svc.confirm_events(db, patient_id, payload.ids, user_id=user.id)
    except svc.MedsError as exc:
        raise _http(exc) from None
    db.commit()
    return MedEventsConfirmResult(confirmed=confirmed)


# Checks (ticket #29): the out-of-date banner. Warnings only, never blocking.


@router.get("/{patient_id}/meds/checks", response_model=MedChecks)
def get_med_checks(
    patient_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> MedChecks:
    """Bitácora entries dated after the current version's «Desde» that mention
    medication changes (their events), minus the dismissed ones (rules in
    ``app.services.meds_checks``)."""
    try:
        svc.get_patient(db, patient_id)
    except svc.MedsError as exc:
        raise _http(exc) from None
    return MedChecks(**meds_checks.outdated_entries(db, patient_id))


@router.post("/{patient_id}/meds/checks/{entry_id}/dismiss", response_model=MedChecks)
def dismiss_med_check(
    patient_id: int,
    entry_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> MedChecks:
    """«Ya está reflejado»: stop reporting this entry, for everyone
    (idempotent, audited). Returns the updated checks. 404 when the entry is
    missing or another patient's."""
    try:
        meds_checks.dismiss(db, patient_id, entry_id, user_id=user.id)
    except svc.MedsError as exc:
        raise _http(exc) from None
    db.commit()
    return MedChecks(**meds_checks.outdated_entries(db, patient_id))
