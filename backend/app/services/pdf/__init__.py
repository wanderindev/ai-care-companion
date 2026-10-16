"""Printable PDFs (DESIGN §6.4 «Imprimir», later §6.6 ``generate_pdf``).

HTML + CSS → PDF with WeasyPrint. Templates live in ``templates/``:
``base.html`` is the shared print layout (Letter page, large type, running
footer with the print date and page numbers) and each document extends it
(``meds.html``, the medication list). A new document is a template that
extends ``base.html`` plus a context builder; :func:`render_pdf` does the rest.

Rules:

- Jinja autoescaping is on: every value is text unless it went through
  :func:`markdown_html` (the ``markdown`` filter), which renders CommonMark +
  tables with **raw HTML disabled** (it shows as text) and images disabled.
- WeasyPrint runs in a **child process** per document (``render.py``, HTML
  on stdin, PDF on stdout): it keeps ~40–70 MB resident in whatever process
  imports it, and the API container is two uvicorn workers in 384 MB. The
  child exits after one document (~0.5 s, peak ~100 MB); one child at a
  time per API process.
- Nothing is fetched while rendering: the URL fetcher refuses every URL, so
  user text cannot make the server request a file or a URL. Fonts come from
  the OS (DejaVu Sans, which has Spanish glyphs and ½ ¼ ¾).
- WeasyPrint's logging is off in the child and its stderr is never logged:
  its warnings can quote document content, and logs never carry medical text.
- :func:`render_pdf` blocks: call it from a sync endpoint (FastAPI runs those
  in its threadpool) or a worker. Nothing is written to disk.
"""

import logging
import re
import subprocess
import sys
import threading
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

TEMPLATES_DIR = Path(__file__).parent / "templates"
RENDERER = Path(__file__).with_name("render.py")
RENDER_TIMEOUT_S = 60

logger = logging.getLogger(__name__)

# One renderer child per API process at a time (each peaks at ~100 MB).
_RENDER_SLOT = threading.BoundedSemaphore(1)

MONTHS_LONG = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

FRACTIONS = {"1/2": "½", "1/4": "¼", "3/4": "¾", "1/3": "⅓", "2/3": "⅔"}
_FRACTION_RE = re.compile(r"(\d+\s+)?\b([123])/([234])\b")


def long_date(value: date | str | None) -> str:
    """``2026-09-30`` → ``30 de septiembre de 2026`` ('' for None)."""
    if not value:
        return ""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.day} de {MONTHS_LONG[value.month - 1]} de {value.year}"


def format_quantity(text: str | None) -> str:
    """``1/2`` → ``½``, ``1 1/2`` → ``1½`` (display only, like the frontend's
    ``formatQuantity``)."""
    if not text:
        return ""

    def glyph(match: re.Match) -> str:
        whole, num, den = match.groups()
        char = FRACTIONS.get(f"{num}/{den}")
        if char is None:
            return match.group(0)
        return f"{whole.strip()}{char}" if whole else char

    return _FRACTION_RE.sub(glyph, text)


@lru_cache(maxsize=1)
def _markdown() -> MarkdownIt:
    # commonmark + GFM tables; html=False escapes raw HTML; no images (they
    # would be fetched) and no linkify.
    return MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")


def markdown_html(text: str | None) -> Markup:
    """Markdown (tables allowed) → safe HTML; raw HTML in the text stays text."""
    if not text:
        return Markup("")
    return Markup(_markdown().render(text))


@lru_cache(maxsize=1)
def environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["long_date"] = long_date
    env.filters["quantity"] = format_quantity
    env.filters["markdown"] = markdown_html
    return env


def render_html(template: str, context: dict[str, Any]) -> str:
    return environment().get_template(template).render(**context)


class PdfRenderError(RuntimeError):
    """The renderer failed or timed out (details are not kept: they may quote
    the document)."""


def render_pdf(template: str, context: dict[str, Any]) -> bytes:
    """Render ``template`` (under ``templates/``) with ``context`` to PDF bytes."""
    html = render_html(template, context)
    try:
        with _RENDER_SLOT:
            result = subprocess.run(
                [sys.executable, "-I", str(RENDERER)],
                input=html.encode("utf-8"),
                capture_output=True,
                timeout=RENDER_TIMEOUT_S,
                check=False,
            )
    except subprocess.TimeoutExpired:
        logger.error("PDF render timed out after %ss", RENDER_TIMEOUT_S)
        raise PdfRenderError("timeout") from None
    if result.returncode != 0 or not result.stdout.startswith(b"%PDF-"):
        logger.error("PDF render failed (exit code %s)", result.returncode)
        raise PdfRenderError(f"exit code {result.returncode}")
    return result.stdout
