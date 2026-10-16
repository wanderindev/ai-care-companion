"""Staging JSON ↔ version 1 of a patient's medication list.

The staging file is the reviewable hand-off between extraction (laptop, API
key) and loading (droplet, no key). It contains PHI: the list's text and what
the model extracted. It never contains the preamble (patient name, ID number).

The operator edits only the ``list`` object (``effective_date``,
``list_note``, ``items``, ``avoid``); each item's ``slot`` is final there
(``section``/``header``/``rescue``/``warnings`` are not saved; ``section``,
``header`` and ``warnings`` feed the review).

Loading creates **version 1** through :func:`app.services.meds.save_list` in
one transaction (every item validated with the API's schemas first; one bad
item loads nothing; text over a column limit is refused, never cut). The
:mod:`review` checks run on the staging file as edited:

- blocking (always refused): an item without a slot (``needs_review``);
- review warnings (refused unless ``accept_warnings``):
  ``possible_missing_items``, ``possible_missing_avoid``,
  ``table_section_manual_check``, ``no_section``, ``unknown_header``,
  ``header_mismatch``, leftover ``bad_*`` extraction codes in an item's
  ``warnings`` (clear them by fixing the field and deleting the code) and
  ``ends_in_past``.

The load is also refused when the patient already has a version, so
re-running never duplicates a list: later changes are made in the app.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session as DbSession

from app.models.patient import Patient
from app.schemas.meds import MedAvoidIn, MedItemIn
from app.services import meds
from app.services.appointments import today_local
from app.services.bitacora_import.load import check_writable, write_staging
from app.services.doctors import match_doctors
from app.services.meds_import.docx_parse import ParsedList
from app.services.meds_import.extract import Extraction, Usage
from app.services.meds_import.review import Review, review_list

__all__ = ["check_writable", "write_staging"]

KIND = "meds"
STAGING_VERSION = 1
CHANGE_SUMMARY = "Lista inicial importada del documento Word."
PHI_NOTICE = (
    "CONTAINS PATIENT HEALTH INFORMATION. Keep it under private/ (gitignored), "
    "never commit or share it, delete it after the import."
)
STAGING_ONLY = ("section", "header", "rescue", "prescriber", "warnings")


class StagingError(ValueError):
    pass


class AlreadyImported(RuntimeError):
    def __init__(self, version_no: int):
        super().__init__(version_no)
        self.version_no = version_no


# ── Staging file ─────────────────────────────────────────────────────────────


def build_staging(
    *,
    source_sha256: str,
    patient_slug: str,
    model: str,
    parsed: ParsedList,
    extraction: Extraction,
    usage: Usage,
) -> dict[str, Any]:
    return {
        "notice": PHI_NOTICE,
        "kind": KIND,
        "version": STAGING_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_sha256": source_sha256,
        "patient": patient_slug,
        "model": model,
        "sections": [
            {
                "number": n,
                "header": s.header,
                "slot": s.slot,
                "time": s.time.strftime("%H:%M") if s.time else None,
                "lines": s.lines,
                "table_rows": s.table_rows,
            }
            for n, s in enumerate(parsed.sections, 1)
        ],
        "skipped_lines": parsed.skipped_lines,
        "repeated_header_lines": parsed.repeated_header_lines,
        "markdown": parsed.markdown,
        "list": extraction.data,
        "error": extraction.error,
        "warnings": [*parsed.warnings, *extraction.warnings],
        "usage": usage.as_dict(),
    }


def read_staging(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            staging = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise StagingError(f"cannot read staging file: {type(exc).__name__}") from None
    if (
        not isinstance(staging, dict)
        or staging.get("kind") != KIND
        or staging.get("version") != STAGING_VERSION
    ):
        raise StagingError("not a medication-list staging file (or an unsupported version)")
    for key in ("source_sha256", "patient", "list"):
        if key not in staging:
            raise StagingError(f"staging file has no {key!r}")
    return staging


def reusable(staging: dict[str, Any], *, source_sha256: str, patient: str, model: str) -> bool:
    """True when ``staging`` already holds a successful extraction of this
    document for this patient and model (no need to pay for another call)."""
    return (
        (staging.get("source_sha256"), staging.get("patient"), staging.get("model"))
        == (source_sha256, patient, model)
        and not staging.get("error")
        and isinstance(staging.get("list"), dict)
    )


# ── Loading ──────────────────────────────────────────────────────────────────


@dataclass
class LoadReport:
    saved: bool = False
    version_no: int | None = None
    items: int = 0
    avoid: int = 0
    by_slot: Counter = field(default_factory=Counter)
    failures: list[str] = field(default_factory=list)  # "item 3: quantity string_too_long"
    review: Review = field(default_factory=Review)
    warnings_accepted: bool = False
    unmatched_prescribers: int = 0


def _errors(prefix: str, exc: ValidationError) -> list[str]:
    """Pydantic errors as location + type only (never the input value)."""
    return [
        f"{prefix}: {'.'.join(str(p) for p in err['loc']) or '-'} {err['type']}"
        for err in exc.errors()
    ]


def _prescriber(db: DbSession, item: dict[str, Any], report: LoadReport) -> dict[str, Any]:
    """Resolve the prescriber name to a doctor (one match only). A name that
    matches none or several is kept visible in the instructions."""
    name = item.get("prescriber")
    if not name:
        return item
    matches = match_doctors(db, name)
    if len(matches) == 1:
        return {**item, "prescriber_id": matches[0].id}
    report.unmatched_prescribers += 1
    note = f"Indicado por: {name}"
    instructions = item.get("instructions")
    return {**item, "instructions": f"{instructions}; {note}" if instructions else note}


def _parse_date(value: Any, report: LoadReport) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        report.failures.append("list: effective_date invalid")
        return None


def load_staging(
    db: DbSession,
    staging: dict[str, Any],
    *,
    patient: Patient,
    user_id: int | None,
    dry_run: bool = False,
    accept_warnings: bool = False,
    today: date | None = None,
) -> LoadReport:
    """Create version 1 from ``staging['list']`` (or preview it with
    ``dry_run``: everything runs, then the transaction is rolled back).

    Raises :class:`AlreadyImported` when the patient already has a version.
    Validation and review problems are returned in ``report.failures``
    (nothing saved).
    """
    report = LoadReport()
    latest = meds.latest_version(db, patient.id)
    if latest is not None:
        raise AlreadyImported(latest.version_no)
    data = staging.get("list")
    if staging.get("error") or not isinstance(data, dict):
        report.failures.append(f"extraction: {staging.get('error') or 'no list'}")
        return report
    report.review = review_list(staging.get("sections") or [], data, today or today_local())
    report.failures.extend(report.review.blocking)
    if report.review.warnings:
        if accept_warnings:
            report.warnings_accepted = True
        else:
            report.failures.extend(f"REVISAR {w}" for w in report.review.warnings)
    effective_date = _parse_date(data.get("effective_date"), report)
    list_note = data.get("list_note")
    if list_note is not None and not isinstance(list_note, str):
        report.failures.append("list: list_note is not text")

    items: list[dict[str, Any]] = []
    for number, raw in enumerate(data.get("items") or [], 1):
        if not isinstance(raw, dict):
            report.failures.append(f"item {number}: not an object")
            continue
        if not raw.get("slot"):
            continue  # reported by the review (needs_review)
        raw = _prescriber(db, raw, report)
        values = {k: v for k, v in raw.items() if k not in STAGING_ONLY}
        try:
            items.append(MedItemIn.model_validate(values).model_dump())
        except ValidationError as exc:
            report.failures.extend(_errors(f"item {number}", exc))
    avoid: list[dict[str, Any]] = []
    for number, raw in enumerate(data.get("avoid") or [], 1):
        try:
            avoid.append(MedAvoidIn.model_validate(raw).model_dump())
        except ValidationError as exc:
            report.failures.extend(_errors(f"avoid {number}", exc))
    if any(i.get("id") or i.get("source_entry_id") for i in [*items, *avoid]):
        report.failures.append("list: ids are not allowed in an import")
    if not items and not avoid and not report.failures:
        report.failures.append("list: no items")
    if report.failures:
        db.rollback()
        return report

    report.items, report.avoid = len(items), len(avoid)
    report.by_slot = Counter(i["slot"] for i in items)
    try:
        outcome = meds.save_list(
            db,
            patient.id,
            items=items,
            avoid=avoid,
            expected_version=0,
            change_summary=CHANGE_SUMMARY,
            dry_run=dry_run,
            user_id=user_id,
            list_note=(list_note or "").strip() or None,
            effective_date=effective_date,
        )
    except meds.MedsError as exc:
        report.failures.append(f"save: {exc}")
        return report
    if outcome.saved:
        db.commit()
        report.saved = True
        report.version_no = outcome.version.version_no
    return report
