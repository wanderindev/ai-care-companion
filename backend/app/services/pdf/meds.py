"""The printable medication list (DESIGN §6.4 «Imprimir»): ``meds.html``.

Both the current list and a historical version are printed from a snapshot
(shape in :mod:`app.services.meds_diff`), so the two always look the same.
The patient's private fields are used only here: ``full_name`` (else the
display name) and ``id_number`` (the «CIP» line is left out when empty).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.models.medication import SLOTS
from app.models.patient import Patient
from app.services import ledd, ledd_substances, pdf
from app.services import meds as svc
from app.services.appointments import to_local, today_local
from app.services.meds_diff import SLOT_LABELS, format_days

TEMPLATE = "meds.html"

# The printed sheet calls the as-needed section «Rescate» too.
PRINT_LABELS = {**SLOT_LABELS, "segun_necesidad": "Según necesidad (rescate)"}


@dataclass
class MedsPrint:
    """What the endpoint needs: the PDF and its (PHI-free) file name."""

    pdf: bytes
    filename: str


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def build_context(
    patient: Patient,
    snapshot: dict[str, Any],
    *,
    version_no: int,
    since: date | None,
    printed_on: date,
    today: date,
    ledd_mg: float | None = None,
) -> dict[str, Any]:
    """Template context for ``meds.html``. ``today`` decides which items show
    «Empieza el …» (a start date after it)."""
    items = snapshot.get("items") or []
    sections = []
    for slot in SLOTS:
        rows = []
        for item in items:
            if item.get("slot") != slot:
                continue
            days = item.get("days")
            start = _parse_date(item.get("start_date"))
            rows.append(
                {
                    **item,
                    "quantity_text": pdf.format_quantity(item.get("quantity")),
                    "days_text": format_days(days) if days else "",
                    "starts_on": start if start and start > today else None,
                    "ends_on": _parse_date(item.get("end_date")),
                }
            )
        if rows:
            sections.append({"slot": slot, "label": PRINT_LABELS[slot], "items": rows})

    footer = f"Impreso el {pdf.long_date(printed_on)}"
    if version_no:
        footer += f" · Versión {version_no}"
    return {
        "title": "Lista de medicamentos",
        "footer_text": footer,
        "patient_name": (patient.full_name or "").strip() or patient.display_name,
        "id_number": (patient.id_number or "").strip() or None,
        "since": since,
        "sections": sections,
        "avoid": snapshot.get("avoid") or [],
        "list_note": (snapshot.get("list_note") or "").strip() or None,
        "ledd_text": format_ledd(ledd_mg),
    }


def format_ledd(total: float | None) -> str | None:
    """«LEDD ≈ 412.5 mg/día» for the header (ticket #133), or None."""
    if total is None:
        return None
    return f"LEDD ≈ {ledd.format_mg(total)} mg/día"


def _since(effective: Any, changed_at: datetime | None) -> date | None:
    """«Desde»: the version's ``effective_date``, else the Panamá day it was saved."""
    return _parse_date(effective) or (to_local(changed_at).date() if changed_at else None)


def print_meds(db: DbSession, patient_id: int, version_no: int | None = None) -> MedsPrint:
    """The patient's current list (or version ``version_no``) as a PDF.
    Raises :class:`app.services.meds.MedsError` (404) for an unknown patient
    or version."""
    patient = svc.get_patient(db, patient_id)
    today = today_local()
    if version_no is None:
        # Same order as GET /meds: version first, then the rows.
        latest = svc.latest_version(db, patient_id)
        snapshot = svc.build_snapshot(
            db, svc.current_items(db, patient_id, today), svc.current_avoid(db, patient_id)
        )
        snapshot["list_note"] = latest.list_note if latest else None
        number = latest.version_no if latest else 0
        since = _since(latest.effective_date, latest.changed_at) if latest else None
        reference = today
    else:
        version = svc.get_version(db, patient_id, version_no)
        snapshot = version["snapshot"]
        number = version["version_no"]
        since = _since(
            version.get("effective_date") or snapshot.get("effective_date"),
            version["changed_at"],
        )
        # As the version page does: «Empieza el» relative to the version's «Desde».
        reference = since or today

    result = ledd_substances.ledd_for_items(db, snapshot.get("items") or [])
    context = build_context(
        patient,
        snapshot,
        version_no=number,
        since=since,
        printed_on=today,
        today=reference,
        ledd_mg=result["total_mg"],
    )
    filename = f"medicamentos-{patient.slug}-{(since or today).isoformat()}.pdf"
    return MedsPrint(pdf=pdf.render_pdf(TEMPLATE, context), filename=filename)
