"""Medication-safety review of a staged list (no AI, no database).

Run after extraction (printed as ``REVISAR``) and again by the load on the
staging file as edited by the operator:

- **blocking** (the load always refuses): an item without a slot, i.e. one
  the model took from the EVITAR section or from no section. It never
  becomes an active medication silently: in the staging file, set its
  ``slot`` or move it to ``avoid``.
- **warnings** (the load refuses unless ``--accept-warnings``):

  - ``possible_missing_items`` (a section has more text lines than items
    taken from it) and ``possible_missing_avoid`` (EVITAR: its text lines
    plus its table data rows against the ``avoid`` entries);
  - ``table_section_manual_check`` (a medication section holds a table: its
    rows may be items or a regimen in ``list_note``, so no count is
    possible; not raised for EVITAR, whose rows are counted instead);
  - ``no_section`` (an item with a slot but no valid section number),
    ``unknown_header`` (the header the model reports is none of the detected
    section headers), ``header_mismatch`` (it is another section's header
    than the one whose number the model gave; the slot comes from the number);
  - any ``bad_*`` code still in an item's ``warnings`` (extraction found an
    unreadable value and dropped it: ``bad_days`` would load as *daily*,
    ``bad_time`` without its hour, ``bad_start_date``/``bad_end_date``
    without the date). The operator clears it by fixing the field in the
    staging file and deleting the code from the item's ``warnings``;
  - ``ends_in_past`` (the item would be saved but not be on the current
    list; a wrong year would silently drop a medicine).

Items are numbered by their position in ``items`` (1-based, every entry
counted), the same numbering as the load's validation messages. Messages
carry section and item numbers only, never medication text.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.services.meds_import.docx_parse import AVOID, SECTION_MARKER, header_text


@dataclass
class Review:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # informational, never gating


def _section_no(value: Any, count: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= count:
        return value
    return None


def _item_checks(
    number: int, item: dict[str, Any], sections: int, keys: list[str], today: date
) -> tuple[list[str], list[str], int | None]:
    """``(blocking, warnings, section number)`` for one item."""
    blocking: list[str] = []
    warnings: list[str] = []
    section = _section_no(item.get("section"), sections)
    if not item.get("slot"):
        blocking.append(
            f"item {number}: needs_review (from EVITAR or from no section; set its slot "
            "or move it to avoid)"
        )
    elif section is None:
        warnings.append(f"item {number}: no_section")
    header = item.get("header")
    if isinstance(header, str) and header.strip():
        key = header_text(SECTION_MARKER.sub("", header))
        if key not in keys:
            warnings.append(f"item {number}: unknown_header")
        elif section is not None and key != keys[section - 1]:
            warnings.append(f"item {number}: header_mismatch section={section}")
    for code in item.get("warnings") or []:
        if isinstance(code, str) and code.startswith("bad_"):
            warnings.append(f"item {number}: {code} (fix the field, then delete the code)")
    end = item.get("end_date")
    try:
        if end and date.fromisoformat(end) < today:
            warnings.append(f"item {number}: ends_in_past (would not be on the current list)")
    except (TypeError, ValueError):
        pass  # the load's validation reports it
    return blocking, warnings, section


def review_list(sections: list[dict[str, Any]], data: dict[str, Any], today: date) -> Review:
    """``sections``: the staging file's ``sections`` (``header``, ``slot``,
    ``lines``, ``table_rows``) in document order; ``data``: its ``list``."""
    review = Review()
    keys = [header_text(s.get("header") or "") for s in sections]
    taken: Counter = Counter()
    for number, item in enumerate(data.get("items") or [], 1):
        if not isinstance(item, dict):
            continue  # the load reports "item N: not an object"
        blocking, warnings, section = _item_checks(number, item, len(sections), keys, today)
        review.blocking.extend(blocking)
        review.warnings.extend(warnings)
        taken[section] += 1

    avoid_lines = 0
    for number, section in enumerate(sections, 1):
        lines = int(section.get("lines") or 0)
        rows = int(section.get("table_rows") or 0)
        if section.get("slot") == AVOID:
            avoid_lines += lines + rows  # an EVITAR table's rows count as EVITAR entries
            continue
        if rows > 0:
            review.warnings.append(f"table_section_manual_check section={number} rows={rows}")
        if taken[number] < lines:
            review.warnings.append(
                f"possible_missing_items section={number} lines={lines} items={taken[number]}"
            )
    avoid = len(data.get("avoid") or [])
    if avoid < avoid_lines:
        review.warnings.append(f"possible_missing_avoid lines={avoid_lines} avoid={avoid}")
    return review
