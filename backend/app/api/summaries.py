"""Ready-made documents (DESIGN §6.6, ticket #34): the «Resumen pre-consulta»
PDF and the «Tarjeta de emergencia» (PDF + the data for its phone screen).

The PDFs are sync endpoints on purpose: rendering waits on a WeasyPrint
child process (``app.services.pdf``), so FastAPI runs them in its
threadpool. The data is copied into plain objects first and the database
connection is given back (``rollback``) before rendering. File names carry
the patient's slug and the date, never a name; nothing is cached; 503 when
the renderer fails.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.summaries import EmergencyCardOut
from app.services import emergency_card as card_svc
from app.services import previsit as previsit_svc
from app.services.pdf import PdfRenderError
from app.services.pdf import summaries as summaries_pdf

router = APIRouter(prefix="/patients", tags=["summaries"])

RENDER_FAILED = "No se pudo generar el PDF. Inténtalo de nuevo."


def _pdf(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _not_found(exc: previsit_svc.PrevisitError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


@router.get(
    "/{patient_id}/previsit.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def previsit_pdf(
    patient_id: int,
    doctor_id: int = Query(ge=1),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> Response:
    """«Resumen pre-consulta» of the patient for the visit with ``doctor_id``
    (rules in :mod:`app.services.previsit`). 404 for an unknown patient or
    doctor."""
    try:
        summary = previsit_svc.previsit_summary(db, patient_id, doctor_id)
    except previsit_svc.PrevisitError as exc:
        raise _not_found(exc) from None
    db.rollback()  # no connection held while the child process renders
    try:
        content = summaries_pdf.render_previsit(summary)
    except PdfRenderError:
        raise HTTPException(status_code=503, detail=RENDER_FAILED) from None
    return _pdf(content, summaries_pdf.previsit_filename(summary))


@router.get(
    "/{patient_id}/emergency-card.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def emergency_card_pdf(
    patient_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> Response:
    """One-page «Tarjeta de emergencia» (with the full name and CIP)."""
    try:
        card = card_svc.emergency_card(db, patient_id)
    except previsit_svc.PrevisitError as exc:
        raise _not_found(exc) from None
    db.rollback()
    try:
        content = summaries_pdf.render_emergency_card(card)
    except PdfRenderError:
        raise HTTPException(status_code=503, detail=RENDER_FAILED) from None
    return _pdf(content, summaries_pdf.emergency_filename(card))


@router.get("/{patient_id}/emergency-card", response_model=EmergencyCardOut)
def emergency_card_data(
    patient_id: int,
    response: Response,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> EmergencyCardOut:
    """The card's data for the phone screen (``/emergencia``)."""
    try:
        card = card_svc.emergency_card(db, patient_id)
    except previsit_svc.PrevisitError as exc:
        raise _not_found(exc) from None
    response.headers["Cache-Control"] = "no-store"
    data = asdict(card)
    data["updated_on"] = data.pop("today")
    return EmergencyCardOut.model_validate(data)
