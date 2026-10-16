"""A free-form document from the chat (DESIGN §6.6 ``generate_pdf``):
``chat_document.html``.

:func:`render_chat_document` takes a title, an optional patient display name
(«Papá» / «Mamá» — never the full name or the CIP) and a list of sections,
each with an optional heading, markdown (rendered by the shared ``markdown``
filter: raw HTML shows as text, no images) and an optional table (plain text
cells). The pre-visit summary and emergency card (#34) have their own
templates (:mod:`app.services.pdf.summaries`).

Inputs are trusted to be validated already (sizes, types): the chat tool
does it in :mod:`app.services.chat.files`.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.services import pdf
from app.services.appointments import to_local

TEMPLATE = "chat_document.html"


@dataclass
class DocTable:
    columns: list[str]
    rows: list[list[str]]


@dataclass
class DocSection:
    heading: str | None = None
    markdown: str | None = None
    table: DocTable | None = None


@dataclass
class ChatDocument:
    title: str
    sections: list[DocSection] = field(default_factory=list)
    patient_name: str | None = None


def build_context(doc: ChatDocument, generated_at: datetime) -> dict[str, Any]:
    local = to_local(generated_at)
    generated = f"{pdf.long_date(local.date())}, {local:%H:%M}"
    return {
        "title": doc.title,
        "patient_name": doc.patient_name,
        "generated": generated,
        "footer_text": f"Generado por el asistente el {generated}",
        "sections": doc.sections,
    }


def render_chat_document(doc: ChatDocument, *, generated_at: datetime | None = None) -> bytes:
    """The document as PDF bytes (WeasyPrint in a child process, blocking).
    Raises :class:`app.services.pdf.PdfRenderError`."""
    context = build_context(doc, generated_at or datetime.now(UTC))
    return pdf.render_pdf(TEMPLATE, context)
