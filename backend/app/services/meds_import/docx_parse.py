"""Medication-list DOCX → markdown → sections (deterministic, no AI).

Document shape (one list per patient, 1–2 pages)::

    LISTA DE MEDICAMENTOS                        ← preamble: dropped, counted
    <patient header: full name and ID number>    ← preamble: dropped, counted
    DESDE 30 SEP 26                              ← the list's date (kept as a date only)
    AL DESPERTAR                                 ← section header → slot
    Medicamento A 250 mg ½ tableta …
    ENTRE 7:30 y 8:00 PM                         ← time-window header → slot by time
    …
    EVITAR                                       ← the EVITAR block
    <optional regimen table>                     ← a markdown table

Section headers are recognized from a fixed table (:data:`HEADERS`, accents,
case, emphasis and a trailing colon ignored) or as a clock time / time window
("ENTRE 7:30 y 8:00 PM", "8:00 PM"). A table row whose only text is a header
counts as one too, and so does a slot word with a qualifier ("DESPUÉS DEL
DESAYUNO", "ANTES DE LA CENA", "ANTES DE ACOSTARSE": the word's slot, the
qualifier goes into each item's instructions). Everything before the first
header is the preamble (title, patient name and ID number): it is **dropped**
and only counted, except that a "DESDE <fecha>" in it gives the list's date
(a DESDE line inside a section stays: it is that item's start date). A
preamble line that is not the title, the name / ID line or DESDE (judged by
its shape only, :func:`preamble_kind`) is reported in
``ParsedList.preamble_unrecognized`` and the CLI aborts before any API call:
a medication is never dropped silently. A preamble line that reappears
verbatim later (a header repeated on page 2) is dropped as well and counted.
A body line that names an ID document ("CIP", "cédula") is reported in
``ParsedList.id_lines`` and the CLI aborts before any API call (PHI guard).
"""

import datetime as dt
import re
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

from docx import Document
from docx.table import Table

from app.services.bitacora_import.docx_parse import (
    header_key,
    paragraph_markdown,
    parse_date_line,
    plain,
)
from app.services.doctors import strip_accents

AVOID = "evitar"  # pseudo-slot of the EVITAR block (rows go to med_avoid)

# Normalized header text (see :func:`header_text`) → slot.
HEADERS: dict[str, str] = {
    "AL DESPERTAR": "al_despertar",
    "CUANDO DESPIERTE": "al_despertar",
    "AL LEVANTARSE": "al_despertar",
    "DESAYUNO": "desayuno",
    "CON EL DESAYUNO": "desayuno",
    "EN EL DESAYUNO": "desayuno",
    "MANANA": "manana",
    "EN LA MANANA": "manana",
    "POR LA MANANA": "manana",
    "ALMUERZO": "almuerzo",
    "CON EL ALMUERZO": "almuerzo",
    "EN EL ALMUERZO": "almuerzo",
    "TARDE": "tarde",
    "EN LA TARDE": "tarde",
    "POR LA TARDE": "tarde",
    "CENA": "cena",
    "CON LA CENA": "cena",
    "EN LA CENA": "cena",
    "NOCHE": "noche",
    "EN LA NOCHE": "noche",
    "POR LA NOCHE": "noche",
    "AL ACOSTARSE": "noche",
    "MADRUGADA": "madrugada",
    "EN LA MADRUGADA": "madrugada",
    "RESCATE": "segun_necesidad",
    "MEDICAMENTOS DE RESCATE": "segun_necesidad",
    "SEGUN NECESIDAD": "segun_necesidad",
    "OTROS MEDICAMENTOS Y SUPLEMENTOS": "otros",
    "OTROS MEDICAMENTOS": "otros",
    "SUPLEMENTOS": "otros",
    "OTROS": "otros",
    "EVITAR": AVOID,
    "MEDICAMENTOS A EVITAR": AVOID,
}

_AMPM = re.compile(r"\b([AP])\s?\.?\s?M\b\.?")
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?"
_TIME_HEADER = re.compile(
    rf"^(?:ENTRE\s+(?:LAS\s+)?|A\s+LAS\s+)?{_CLOCK}"
    rf"(?:\s*(?:Y|A|-|–)\s*(?:LAS\s+)?{_CLOCK})?$"
)
# A slot word with a qualifier that changes when it is taken: "DESPUÉS DEL
# DESAYUNO", "ANTES DE LA CENA", "ANTES DE ACOSTARSE". The slot is the word's;
# the qualifier, as written, goes into each item's instructions. No amount
# ("30 MIN ANTES DEL …"): that reads as an item's instruction, not a header.
_QUALIFIED = re.compile(
    r"^(?:ANTES|DESPUES|LUEGO)\s+(?:(?:DEL?|AL)\s+)?(?:(?:LA|EL)\s+)?(?P<head>.+)$"
)
# Words that only name a slot after a qualifier ("ANTES DE ACOSTARSE").
_QUALIFIED_HEADS = {"ACOSTARSE": "noche", "DORMIR": "noche"}
_NOT_QUALIFIABLE = {AVOID, "otros", "segun_necesidad"}
_PARENS = re.compile(r"^(?P<head>.*?)\s*\((?P<inner>[^()]*)\)$")
_EDGE_PUNCT = " :.;,-–—*_"
_DESDE_LINE = re.compile(r"^(?P<prefix>.*?)\bDESDE\b[\s:]*(?P<rest>.+)$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})$")
_ID_WORDS = re.compile(r"\b(?:CIP|C\.I\.P|CEDULA)\b")
_TABLE_SEPARATOR = re.compile(r"^\|(?:\s*-+\s*\|)+$")
# Preamble lines that may be dropped (see :func:`preamble_kind`).
_TITLE_WORDS = re.compile(r"\b(?:LISTA|MEDICAMENTOS|MEDICINAS|MEDICACION)\b")  # plural only
_ID_NUMBER = re.compile(r"\b[0-9A-Z]{1,4}-\d{1,4}-\d{3,6}\b")  # cédula shape, not "1-0-1"
# Words of an ID line that are not part of a name ("Nombre: … CIP No. …").
_ID_LABELS = {"CIP", "C.I.P", "CEDULA", "NOMBRE", "PACIENTE", "NO", "N", "DE", "Y"}
_NAME_LINE = re.compile(r"^[A-Z .,'-]+(?::\s*[A-Z .,'-]+)?$")
_NAME_MAX_WORDS = 8


@dataclass(frozen=True)
class Header:
    """A recognized section header."""

    slot: str  # a SLOTS value, or AVOID
    time: dt.time | None = None  # start of a clock-time header
    window: str | None = None  # the time as written, e.g. "Entre 7:30 y 8:00 PM"
    note: str | None = None  # a qualifier as written, e.g. "Después del desayuno"


# ── Header and date recognition ──────────────────────────────────────────────


def header_text(text: str) -> str:
    """Comparison form: no markdown, no accents, upper case, single spaces,
    surrounding punctuation (``:``, ``-``…) removed, "A.M." → "AM"."""
    value = strip_accents(plain(text)).upper()
    value = _AMPM.sub(lambda m: f"{m[1]}M", value)
    return " ".join(value.split()).strip(_EDGE_PUNCT)


def _clock(hour: str, minute: str | None, ampm: str | None) -> time | None:
    h, m = int(hour), int(minute or 0)
    if ampm == "PM" and h < 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    if h > 23 or m > 59:
        return None
    return time(h, m)


def slot_for_time(value: time) -> str:
    """A clock-time header is not a meal: madrugada (< 5:00), mañana (< 12:00),
    tarde (< 19:00) or noche."""
    if value.hour < 5:
        return "madrugada"
    if value.hour < 12:
        return "manana"
    if value.hour < 19:
        return "tarde"
    return "noche"


def _window_label(text: str) -> str:
    """ "ENTRE 7:30 y 8:00 P.M." → "Entre 7:30 y 8:00 PM"."""
    value = " ".join(plain(text).split()).strip(_EDGE_PUNCT).lower()
    value = re.sub(r"\b([ap])\s?\.?\s?m\b\.?", lambda m: f"{m[1].upper()}M", value)
    return value[:1].upper() + value[1:]


def _time_header(key: str, original: str) -> Header | None:
    m = _TIME_HEADER.match(key)
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    if not (ap1 or ap2 or m1):  # a bare number is not a time
        return None
    start = _clock(h1, m1, ap1 or ap2)
    if start is None or (h2 and _clock(h2, m2, ap2 or ap1) is None):
        return None
    return Header(slot=slot_for_time(start), time=start, window=_window_label(original))


def _named_header(key: str, original: str) -> Header | None:
    """A header from the table, or a slot word with a qualifier ("DESPUÉS DEL
    DESAYUNO": slot ``desayuno``, ``note`` "Después del desayuno")."""
    if key in HEADERS:
        return Header(slot=HEADERS[key])
    m = _QUALIFIED.match(key)
    if not m:
        return None
    slot = HEADERS.get(m["head"]) or _QUALIFIED_HEADS.get(m["head"])
    if slot is None or slot in _NOT_QUALIFIABLE:
        return None
    return Header(slot=slot, note=_window_label(original))


def classify_header(text: str | None) -> Header | None:
    """The header ``text`` names, or None. Also used for the header the model
    reports for each item, so the slot mapping is always this table."""
    if not text:
        return None
    key = header_text(text)
    if not key:
        return None
    found = _named_header(key, text) or _time_header(key, text)
    if found:
        return found
    m = _PARENS.match(key)  # "DESAYUNO (7:00 AM)"
    if not m:
        return None
    original = _PARENS.match(" ".join(plain(text).split()).strip(_EDGE_PUNCT))
    named = _named_header(m["head"], original["head"] if original else m["head"])
    if named is None:
        return None
    inner = _time_header(m["inner"], m["inner"])
    if inner and named.slot != AVOID:
        return Header(slot=named.slot, time=inner.time, window=inner.window, note=named.note)
    return named


def parse_since(rest: str) -> date | None:
    """The date after "DESDE": "30 SEP 26", "30 de septiembre de 2026", "30/09/2026"."""
    value = re.sub(r"^EL\s+", "", header_text(rest))
    value = re.sub(r"\bDEL?\b", " ", value)
    value = " ".join(value.split())
    found = parse_date_line(value)
    if found:
        return found
    m = _NUMERIC_DATE.match(value)
    if not m:
        return None
    year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
    try:
        return date(year, int(m[2]), int(m[1]))
    except ValueError:
        return None


# ── DOCX → markdown ──────────────────────────────────────────────────────────


def _cell_text(cell) -> str:
    text = " ".join(line for p in cell.paragraphs for line in paragraph_markdown(p) if line)
    return text.replace("|", "\\|")


def table_lines(table: Table) -> list[str]:
    """A table as markdown rows; horizontally merged cells appear once and
    short rows are padded, the first row is the table's header row."""
    rows: list[list[str]] = []
    for row in table.rows:
        cells, seen = [], set()
        for cell in row.cells:
            if id(cell._tc) in seen:
                continue
            seen.add(id(cell._tc))
            cells.append(_cell_text(cell))
        if any(cells):
            rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    lines = []
    for i, cells in enumerate(rows):
        cells = cells + [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + " --- |" * width)
    return lines


def docx_to_markdown_lines(path: Path) -> list[str]:
    doc = Document(str(path))
    lines: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Table):
            lines.extend(table_lines(block))
        else:
            lines.extend(paragraph_markdown(block))
        lines.append("")
    return lines


# ── Sections ─────────────────────────────────────────────────────────────────


@dataclass
class Section:
    header: str  # as written (plain text, no markdown)
    slot: str
    time: dt.time | None
    window: str | None
    lines: int = 0  # non-blank, non-table body lines under it
    table_rows: int = 0  # markdown table data rows under it (no header/separator rows)
    note: str | None = None  # the header's qualifier ("Después del desayuno")


SECTION_MARKER = re.compile(r"^\s*\[S\d+\]\s*")


@dataclass
class ParsedList:
    # What Claude gets: from the first header on; each header line is
    # prefixed with its section marker "[S<n>] " (1-based).
    lines: list[str]
    sections: list[Section]
    effective_date: date | None
    skipped_lines: int  # preamble (title, patient header): content never kept
    repeated_header_lines: int  # preamble lines found again later, dropped
    id_lines: list[int] = field(default_factory=list)  # PHI guard (1-based, in ``lines``)
    # Preamble lines that are not the title, the patient's name / ID line or
    # DESDE (1-based among the document's non-blank lines): possibly dropped
    # medications or an unrecognized header, so the CLI stops before any call.
    preamble_unrecognized: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def markdown(self) -> str:
        return "\n".join(self.lines)

    def section_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for section in self.sections:
            counts[section.slot] = counts.get(section.slot, 0) + 1
        return counts


def _row_header(line: str) -> str:
    """The text a line could be a header with: a table row counts when it has
    a single distinct non-empty cell."""
    if not line.startswith("|"):
        return line
    cells = {c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|")) if c.strip()}
    return next(iter(cells)) if len(cells) == 1 else ""


def _trim(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def preamble_since(key: str) -> date | None:
    """The list's date when ``key`` (:func:`header_text` form) is a DESDE line:
    "DESDE <fecha>", optionally after a title ("LISTA DE MEDICAMENTOS DESDE
    …"), the whole rest a date. A medication's "… 100 MG DESDE 1/10/26" is not."""
    m = _DESDE_LINE.match(key)
    if not m:
        return None
    prefix = m["prefix"].strip(_EDGE_PUNCT)
    if prefix and (any(c.isdigit() for c in prefix) or not _TITLE_WORDS.search(prefix)):
        return None
    return parse_since(m["rest"])


def _is_id_line(key: str) -> bool:
    """Names an ID document, or is letters plus one cédula-shaped number that
    is not a date (and no other digits)."""
    if _ID_WORDS.search(key):
        return True
    numbers = _ID_NUMBER.findall(key)
    if len(numbers) != 1 or parse_since(numbers[0]) is not None:
        return False
    return not any(c.isdigit() for c in _ID_NUMBER.sub(" ", key))


def _id_line_has_name(key: str) -> bool:
    """An ID line that also carries the name ("FULANO … CIP 0-000-0000")."""
    words = re.sub(r"[:;,|]", " ", _ID_NUMBER.sub(" ", key)).split()
    return any(
        w.strip(".") not in _ID_LABELS and w.replace(".", "").isalpha() and len(w) > 1
        for w in words
    )


def preamble_kind(line: str, *, seen: set[str]) -> str | None:
    """What a preamble line is, judged by its shape only (never real names):
    ``"blank"`` (a table separator), ``"desde"`` (see :func:`preamble_since`),
    ``"id"`` (see :func:`_is_id_line`), ``"title"`` ("LISTA DE MEDICAMENTOS",
    no digits), ``"name"`` (letters only, a few words) or None: anything else,
    e.g. a medication or an unknown header. ``seen`` holds the kinds already
    found (``"name"`` also once an ID line carries a name): one DESDE, one ID
    line and one name line, the name before DESDE; a second one is None."""
    key = header_text(_row_header(line) or line)
    if not key or _TABLE_SEPARATOR.match(line):
        return "blank"
    if preamble_since(key) is not None:
        return "desde" if "desde" not in seen else None
    if _DESDE_LINE.match(key):
        return None  # a DESDE whose date cannot be read: maybe an item's
    if _is_id_line(key):
        return "id" if "id" not in seen else None
    if any(c.isdigit() for c in key):
        return None
    if _TITLE_WORDS.search(key):
        return "title"
    if (
        not seen & {"name", "desde"}
        and _NAME_LINE.match(key)
        and len(key.split()) <= _NAME_MAX_WORDS
    ):
        return "name"
    return None


def split_document(lines: list[str]) -> ParsedList:
    """Split the markdown lines into preamble (dropped) and sections.

    The list's date is a "DESDE <fecha>" **in the preamble only**: a DESDE
    line inside a section stays in the body (it is that item's start date).
    Every dropped preamble line must be the title, the patient's name / ID
    line or DESDE (:func:`preamble_kind`; at most one of each, the name
    before DESDE); anything else is listed in ``preamble_unrecognized`` so a medication is
    never dropped silently.
    """
    body: list[str] = []
    sections: list[Section] = []
    dropped: set[str] = set()
    skipped = repeated = 0
    since: list[date] = []
    warnings: list[str] = []
    unrecognized: list[int] = []
    seen: set[str] = set()  # preamble kinds found so far
    position = 0  # 1-based among non-blank lines

    last_counted_row = False  # the previous line was a table row counted in table_rows
    for line in lines:
        previous_counted_row, last_counted_row = last_counted_row, False
        if line:
            position += 1
        if not sections:  # preamble
            header = classify_header(_row_header(line)) if line else None
            if header is None:
                if line:
                    skipped += 1
                    dropped.add(header_key(line))
                    kind = preamble_kind(line, seen=seen)
                    if kind is None:
                        unrecognized.append(position)
                        continue
                    seen.add(kind)
                    key = header_text(_row_header(line) or line)
                    if kind == "id" and _id_line_has_name(key):
                        seen.add("name")
                    if kind == "desde":
                        since.append(preamble_since(key))
                continue
        elif not line:
            body.append(line)
            continue
        else:
            if header_key(line) in dropped:
                repeated += 1
                continue
            header = None if _TABLE_SEPARATOR.match(line) else classify_header(_row_header(line))
        if header is not None:
            sections.append(
                Section(
                    header=" ".join(plain(_row_header(line)).split()).strip(_EDGE_PUNCT),
                    slot=header.slot,
                    time=header.time,
                    window=header.window,
                    note=header.note,
                )
            )
            body.append(f"[S{len(sections)}] {line}")
            continue
        if _TABLE_SEPARATOR.match(line):
            # The row right above was the table's header row: uncount it, but
            # only if it was counted here (a header row that is itself a
            # section header, e.g. "| DESAYUNO | DESAYUNO |", never was).
            if previous_counted_row:
                sections[-1].table_rows -= 1
        elif line.startswith("|"):
            sections[-1].table_rows += 1
            last_counted_row = True
        else:
            sections[-1].lines += 1
        body.append(line)

    body = _trim(body)
    id_lines = [i for i, line in enumerate(body, 1) if _ID_WORDS.search(header_text(line))]
    if not since:
        warnings.append("no_since_date")
    return ParsedList(
        lines=body,
        sections=sections,
        effective_date=since[0] if since else None,
        skipped_lines=skipped,
        repeated_header_lines=repeated,
        id_lines=id_lines,
        preamble_unrecognized=unrecognized,
        warnings=warnings,
    )
