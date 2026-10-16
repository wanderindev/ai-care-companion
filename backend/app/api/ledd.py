"""LEDD review API (ticket #133): medication names outside the reference
table that Claude classified (``app.services.ledd_substances``). Admins
confirm a proposal (it then counts toward the LEDD with its factor) or say it
does not count. Every decision is audited (entity ``ledd_substance``)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_admin, require_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.meds import LeddConfirm, LeddSubstanceOut
from app.services import ledd, ledd_substances

router = APIRouter(prefix="/ledd", tags=["meds"])


def _out(row) -> LeddSubstanceOut:
    out = LeddSubstanceOut.model_validate(row)
    out.needs_review = ledd.needs_review(ledd_substances.to_learned(row))
    return out


def _http(exc: ledd_substances.LeddError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/reference")
def get_reference(_user: User = Depends(require_user)) -> dict:
    """The built-in conversion table (substance, factor, source, note) and its version."""
    return {"table_version": ledd.TABLE_VERSION, "substances": ledd.catalogue_rows()}


@router.get("/substances", response_model=list[LeddSubstanceOut])
def list_substances(
    review: bool = Query(default=False, description="Only the ones waiting for an admin."),
    db: DbSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[LeddSubstanceOut]:
    return [_out(r) for r in ledd_substances.list_substances(db, review_only=review)]


@router.post("/substances/{substance_id}/confirm", response_model=LeddSubstanceOut)
def confirm_substance(
    substance_id: int,
    payload: LeddConfirm,
    db: DbSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> LeddSubstanceOut:
    """Count it toward the LEDD with the proposed factor (or ``factor``)."""
    try:
        row = ledd_substances.confirm(db, substance_id, factor=payload.factor, user_id=admin.id)
    except ledd_substances.LeddError as exc:
        db.rollback()
        raise _http(exc) from None
    db.commit()
    return _out(row)


@router.post("/substances/{substance_id}/reject", response_model=LeddSubstanceOut)
def reject_substance(
    substance_id: int,
    db: DbSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> LeddSubstanceOut:
    """It does not count toward the LEDD."""
    try:
        row = ledd_substances.reject(db, substance_id, user_id=admin.id)
    except ledd_substances.LeddError as exc:
        db.rollback()
        raise _http(exc) from None
    db.commit()
    return _out(row)
