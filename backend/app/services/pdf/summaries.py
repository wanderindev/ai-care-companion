"""The ready-made PDFs of DESIGN §6.6 (ticket #34): ``previsit.html`` («Resumen
pre-consulta») and ``emergency_card.html`` («Tarjeta de emergencia»).

The data comes from :mod:`app.services.previsit` and
:mod:`app.services.emergency_card` as plain dataclasses, so the callers
(the API and the chat tools) give the database connection back before
calling these renderers: WeasyPrint runs in a child process
(:func:`app.services.pdf.render_pdf`), blocking.

File names carry the patient's slug and the date, never a name.
"""

from datetime import date
from typing import Any

from app.services import pdf
from app.services.emergency_card import EmergencyCard
from app.services.pdf.meds import PRINT_LABELS
from app.services.previsit import MedRow, PrevisitSummary

PREVISIT_TEMPLATE = "previsit.html"
EMERGENCY_TEMPLATE = "emergency_card.html"


def previsit_filename(summary: PrevisitSummary) -> str:
    return f"resumen-preconsulta-{summary.patient_slug}-{summary.today.isoformat()}.pdf"


def emergency_filename(card: EmergencyCard) -> str:
    return f"tarjeta-emergencia-{card.patient_slug}-{card.today.isoformat()}.pdf"


def _med_text(m: MedRow) -> dict[str, Any]:
    name = " ".join(p for p in (m.name, m.strength) if p)
    return {
        "name": name,
        "quantity": pdf.format_quantity(m.quantity),
        "dose_note": m.dose_note,
        "slot": PRINT_LABELS.get(m.slot, m.slot),
        "time": m.time,
        "instructions": m.instructions,
        "schedule_note": m.schedule_note,
    }


def previsit_context(summary: PrevisitSummary, printed_on: date | None = None) -> dict[str, Any]:
    printed_on = printed_on or summary.today
    doctor = summary.doctor
    return {
        "title": "Resumen pre-consulta",
        "footer_text": f"Resumen pre-consulta · {summary.patient_name} · "
        f"generado el {pdf.long_date(printed_on)}",
        "s": summary,
        "doctor_line": " · ".join(p for p in (doctor.name, doctor.specialty) if p),
        "meds": [_med_text(m) for m in summary.meds],
    }


def emergency_context(card: EmergencyCard) -> dict[str, Any]:
    updated = pdf.long_date(card.today)
    return {
        "title": "Tarjeta de emergencia",
        "footer_text": f"Tarjeta de emergencia — actualizada el {updated}",
        "updated": updated,
        "c": card,
        "name": card.full_name or card.display_name,
    }


def render_previsit(summary: PrevisitSummary) -> bytes:
    """Raises :class:`app.services.pdf.PdfRenderError`."""
    return pdf.render_pdf(PREVISIT_TEMPLATE, previsit_context(summary))


def render_emergency_card(card: EmergencyCard) -> bytes:
    """Raises :class:`app.services.pdf.PdfRenderError`."""
    return pdf.render_pdf(EMERGENCY_TEMPLATE, emergency_context(card))
