"""DOCX → markdown lines → dated entries (deterministic, no AI).

Markdown keeps the emphasis the family uses: bold as ``**text**`` and
underline as ``<u>text</u>`` (plain markdown has no underline). Paragraphs
are separated by a blank line (markdown paragraphs); a soft line break inside
a paragraph is a single newline. Paragraphs that hold only whitespace or
non-breaking spaces add nothing.

Document shape (one section per year, entries newest first, not strictly)::

    BITACORA 2026
    <patient header: full name and ID number>   ← skipped, never stored
    22 SEP 2026                                  ← date line, starts an entry
    **Dra. X**                                   ← usually the doctor/source
    Text…

Everything between a year header and its first date line (the patient header)
is dropped and only counted. Any line starting with "BITACORA" is a section
header, never entry text, even when it is not exactly ``BITACORA <year>``. If
a dropped header line reappears verbatim inside an entry, the split reports it
(``SplitResult.leaked_entries``) and the CLI aborts before any API call. Date lines are a day, a Spanish month
abbreviation and a 2- or 4-digit year, alone on the line (``22 SEP 2026``,
``09 JUL 26``, ``17 DIC 21``).
"""

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_UNDERLINE
from docx.table import Table
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph

from app.services.doctors import strip_accents

MONTHS = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "SET": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
    # English spellings that differ, tolerated as typos.
    "JAN": 1,
    "APR": 4,
    "AUG": 8,
    "DEC": 12,
}

_DATE_LINE = re.compile(
    r"^(?P<day>\d{1,2})[\s./-]+(?P<month>[^\W\d_]{3,10})\.?[\s./-]+(?P<year>\d{4}|\d{2})[.:]?$"
)
_SECTION_HEADER = re.compile(r"^\W*BIT[AÁ]CORA\b", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})(?!\d)")
_MARKUP = re.compile(r"\*\*|</?u>")
_SPACES = re.compile(r"[ \t   ]+")

# ── DOCX → markdown ──────────────────────────────────────────────────────────


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_bold(run, paragraph: Paragraph) -> bool:
    if run.bold is not None:
        return bool(run.bold)
    style = paragraph.style
    return bool(style is not None and style.font is not None and style.font.bold)


def _is_underlined(run) -> bool:
    return run.underline not in (None, False, WD_UNDERLINE.NONE)


def _wrap(text: str, bold: bool, underline: bool) -> str:
    """Wrap one line fragment; surrounding whitespace stays outside the markers."""
    core = text.strip()
    if not core or not (bold or underline):
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    if underline:
        core = f"<u>{core}</u>"
    if bold:
        core = f"**{core}**"
    return f"{lead}{core}{trail}"


def _runs(paragraph: Paragraph) -> Iterator:
    for item in paragraph.iter_inner_content():
        if isinstance(item, Hyperlink):
            yield from item.runs
        else:
            yield item


def paragraph_markdown(paragraph: Paragraph) -> list[str]:
    """One paragraph as markdown lines (a soft line break starts a new line)."""
    groups: list[tuple[bool, bool, str]] = []
    for run in _runs(paragraph):
        text = run.text
        if not text:
            continue
        fmt = (_is_bold(run, paragraph), _is_underlined(run))
        if groups and groups[-1][:2] == fmt:
            groups[-1] = (*fmt, groups[-1][2] + text)
        else:
            groups.append((*fmt, text))
    lines = [""]
    for bold, underline, text in groups:
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i:
                lines.append("")
            lines[-1] += _wrap(part.replace("\t", " "), bold, underline)
    return [_clean_line(line) for line in lines]


def _clean_line(line: str) -> str:
    line = _SPACES.sub(" ", line).strip()
    return line if plain(line) else ""


def _table_lines(table: Table) -> list[str]:
    lines = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = " ".join(line for p in cell.paragraphs for line in paragraph_markdown(p) if line)
            if text and (not cells or cells[-1] != text):  # merged cells repeat
                cells.append(text)
        if cells:
            lines.append(" | ".join(cells))
    return lines


def docx_to_markdown_lines(path: Path) -> list[str]:
    doc = Document(str(path))
    lines: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Table):
            lines.extend(_table_lines(block))
        else:
            lines.extend(paragraph_markdown(block))
        lines.append("")  # paragraph break; runs of blanks collapse in _trim
    return lines


# ── Splitting ────────────────────────────────────────────────────────────────


def plain(line: str) -> str:
    """Line without markdown markers and with whitespace normalized."""
    return _SPACES.sub(" ", _MARKUP.sub("", line)).strip()


def parse_date_line(line: str) -> date | None:
    """The date when ``line`` is a date line alone, else None."""
    m = _DATE_LINE.match(plain(line))
    if not m:
        return None
    month = MONTHS.get(strip_accents(m["month"]).upper()[:3])
    if month is None:
        return None
    year = int(m["year"])
    if year < 100:
        year += 2000
    try:
        return date(year, month, int(m["day"]))
    except ValueError:
        return None


def is_section_header(line: str) -> bool:
    """Any line that starts with "BITACORA" (emphasis and accents allowed)."""
    return bool(_SECTION_HEADER.match(plain(line)))


def parse_year_header(line: str) -> int | None:
    """The year of a section header line (None when it names no year)."""
    if not is_section_header(line):
        return None
    m = _YEAR.search(plain(line))
    return int(m["year"]) if m else None


def header_key(line: str) -> str:
    """Comparison form of a dropped header line (accents, case, spacing)."""
    return " ".join(strip_accents(plain(line)).casefold().split())


@dataclass
class RawEntry:
    """One dated entry as split from the document (markdown, no AI)."""

    index: int
    section_year: int | None
    date_line: str
    entry_date: date
    lines: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def markdown(self) -> str:
        return "\n".join(self.lines)

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(f"{self.date_line}\n{self.markdown}".encode()).hexdigest()


@dataclass
class SplitResult:
    entries: list[RawEntry]
    years: list[int | None]  # one per section header, None when it has no year
    skipped_lines: int  # preamble/patient-header lines; content never kept
    # Entries containing a verbatim copy of a dropped header line (PHI guard).
    leaked_entries: list[int] = field(default_factory=list)

    def counts_by_section(self) -> list[tuple[int | None, int]]:
        """Entries per section, in document order."""
        counts: dict[int | None, int] = {}
        for year in self.years:
            counts.setdefault(year, 0)
        for entry in self.entries:
            counts[entry.section_year] = counts.get(entry.section_year, 0) + 1
        return list(counts.items())


def _trim(lines: list[str]) -> list[str]:
    """Drop leading/trailing blank lines and collapse runs of blank lines."""
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def split_entries(lines: Iterable[str]) -> SplitResult:
    entries: list[RawEntry] = []
    years: list[int | None] = []
    skipped = 0
    dropped: set[str] = set()  # header_key of every dropped line
    section: int | None = None
    current: RawEntry | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            current.lines = _trim(current.lines)
            if not current.lines:
                current.warnings.append("empty_entry")
            entries.append(current)
            current = None

    for line in lines:
        if is_section_header(line):
            # Never entry text, whatever follows "BITACORA" (it may carry the
            # patient's name); the lines up to the next date line are dropped.
            close()
            section = parse_year_header(line)
            years.append(section)
            dropped.add(header_key(line))
            continue
        entry_date = parse_date_line(line)
        if entry_date is not None:
            close()
            current = RawEntry(
                index=len(entries),
                section_year=section,
                date_line=plain(line),
                entry_date=entry_date,
                lines=[],
            )
            if section is not None and entry_date.year != section:
                current.warnings.append("date_outside_year_section")
            continue
        if current is None:
            if line:
                skipped += 1  # patient header / preamble: counted, never kept
                dropped.add(header_key(line))
            continue
        current.lines.append(line)
    close()
    dropped.discard("")
    leaked = [
        entry.index
        for entry in entries
        if any(header_key(line) in dropped for line in entry.lines if line)
    ]
    return SplitResult(entries=entries, years=years, skipped_lines=skipped, leaked_entries=leaked)


# ── Header hint (first line: doctor / source / channel) ─────────────────────

_TITLE = re.compile(r"^(dra?|doctora?|lic|licda|licdo|licenciad[ao])\b\.?", re.IGNORECASE)
_CHANNEL_SUFFIX = re.compile(
    r"\s*[-–—(]\s*(?P<chan>whats\s?app|wa|correo(?:\s+electr[oó]nico)?|e-?mail|mail|"
    r"tel[eé]fono|llamada(?:\s+telef[oó]nica)?|telef[oó]nica|domicilio|"
    r"visita\s+domiciliaria|presencial)\s*\)?\s*$",
    re.IGNORECASE,
)
CHANNEL_WORDS = {
    "whatsapp": "whatsapp",
    "wa": "whatsapp",
    "correo": "correo",
    "email": "correo",
    "mail": "correo",
    "telefono": "telefono",
    "llamada": "telefono",
    "telefonica": "telefono",
    "domicilio": "domicilio",
    "visita": "domicilio",
    "presencial": "presencial",
}
MAX_HEADER = 80


@dataclass
class HeaderHint:
    """What the first line suggests; the model decides, this only guides it."""

    header: str | None = None  # the doctor/source as written (no channel suffix)
    has_title: bool = False  # starts with Dr./Dra./Lic.…
    channel: str | None = None  # from an explicit suffix such as "- WhatsApp"

    def as_dict(self) -> dict:
        return {"header": self.header, "has_title": self.has_title, "channel": self.channel}


def _channel_of(word: str) -> str | None:
    key = strip_accents(word).casefold().replace(" ", "").replace("-", "")
    for prefix, channel in CHANNEL_WORDS.items():
        if key.startswith(prefix):
            return channel
    return None


def header_hint(entry_lines: list[str]) -> HeaderHint:
    first = next((line for line in entry_lines if line), "")
    text = plain(first)
    if not text:
        return HeaderHint()
    candidate: str | None = None
    head, sep, _rest = text.partition(":")
    if sep and len(head.strip()) <= MAX_HEADER and (_TITLE.match(head) or not _rest.strip()):
        candidate = head.strip()  # "Dr. Z: texto" or "Dr. Z:" (older entries)
    elif len(text) <= MAX_HEADER:
        stripped = first.strip()
        whole_bold = (
            stripped.startswith("**") and stripped.endswith("**") and stripped.count("**") == 2
        )
        ends_sentence = text.endswith(".") and not _TITLE.fullmatch(text.split()[-1])
        if whole_bold or _TITLE.match(text) or not ends_sentence:
            candidate = text
    if candidate is None:
        return HeaderHint()
    channel = None
    m = _CHANNEL_SUFFIX.search(candidate)
    if m and m.start() > 0:
        channel = _channel_of(m["chan"])
        candidate = candidate[: m.start()].strip()
    candidate = candidate.rstrip(" :-–—")
    return HeaderHint(
        header=candidate or None, has_title=bool(_TITLE.match(candidate)), channel=channel
    )
