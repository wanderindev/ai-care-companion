"""Staging JSON ↔ ``log_entries`` rows.

The staging file is the reviewable hand-off between extraction (which costs
API calls) and loading (which writes the database). It contains PHI: the
original text of every entry and what the model extracted. It never contains
the patient header (full name, ID number) the document repeats per year.

Loading is idempotent: each row's ``origin_ref`` carries ``source_sha256``,
``entry_index`` and ``text_sha256``, and an entry whose triple already exists
is skipped (so editing an already-loaded entry in the staging file has no
effect on a re-load: edit it in the app). Each entry is committed on its own, so one invalid entry does not
abort the rest.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.models.doctor import Doctor
from app.models.log_entry import REVIEW_PENDING, LogEntry
from app.models.patient import Patient
from app.services import audit
from app.services.bitacora_import.docx_parse import HeaderHint, RawEntry, SplitResult
from app.services.bitacora_import.extract import Extraction, Usage
from app.services.doctors import match_doctors, normalize_name
from app.services.log_entries import LogEntryError, create_entry

ORIGIN = "import_docx"
STAGING_VERSION = 1
PHI_NOTICE = (
    "CONTAINS PATIENT HEALTH INFORMATION. Keep it under private/ or exports/ "
    "(gitignored), never commit or share it, delete it after the import."
)


class StagingError(ValueError):
    pass


# ── Staging file ─────────────────────────────────────────────────────────────


def build_staging(
    *,
    source_sha256: str,
    patient_slug: str,
    model: str,
    split: SplitResult,
    items: list[tuple[RawEntry, HeaderHint]],
    extractions: dict[int, Extraction],
    usage: Usage,
) -> dict[str, Any]:
    entries = []
    for raw, hint in items:
        ext = extractions.get(raw.index) or Extraction(raw.index, error="not_extracted")
        entries.append(
            {
                "entry_index": raw.index,
                "text_sha256": raw.text_sha256,
                "section_year": raw.section_year,
                "date_line": raw.date_line,
                "entry_date": raw.entry_date.isoformat(),
                "markdown": raw.markdown,
                "header_hint": hint.as_dict(),
                "extraction": ext.data,
                "error": ext.error,
                "warnings": [*raw.warnings, *ext.warnings],
            }
        )
    return {
        "notice": PHI_NOTICE,
        "version": STAGING_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_sha256": source_sha256,
        "patient": patient_slug,
        "model": model,
        "years": split.years,
        "total_entries": len(split.entries),
        "entries": entries,
        "usage": usage.as_dict(),
    }


def write_staging(path: Path, staging: dict[str, Any]) -> None:
    """Write atomically (temp file + rename), owner-only (0600) even when
    replacing an existing file: it holds PHI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(staging, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def check_writable(path: Path) -> None:
    """Create the directory and make sure the staging file can be written,
    before spending anything on API calls. Raises ``StagingError``."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.with_name(f".{path.name}.probe")
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        probe.unlink()
        if path.exists() and not os.access(path, os.W_OK):
            raise PermissionError
    except OSError as exc:
        raise StagingError(f"cannot write the staging file {path}: {type(exc).__name__}") from None


def resumable(staging: dict[str, Any], *, source_sha256: str, patient: str, model: str):
    """Extractions a previous (possibly interrupted) run already paid for,
    keyed by ``(entry_index, text_sha256)``; only from a staging file for the
    same document, patient and model (None otherwise), and only those without
    an error."""
    if (staging.get("source_sha256"), staging.get("patient"), staging.get("model")) != (
        source_sha256,
        patient,
        model,
    ):
        return None
    return {
        (e["entry_index"], e["text_sha256"]): e
        for e in staging["entries"]
        if e.get("extraction") and not e.get("error")
    }


def read_staging(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            staging = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise StagingError(f"cannot read staging file: {type(exc).__name__}") from None
    if not isinstance(staging, dict) or staging.get("version") != STAGING_VERSION:
        raise StagingError("not a bitácora staging file (or an unsupported version)")
    for key in ("source_sha256", "patient", "entries"):
        if key not in staging:
            raise StagingError(f"staging file has no {key!r}")
    return staging


# ── Loading ──────────────────────────────────────────────────────────────────


@dataclass
class LoadReport:
    new: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[tuple[int, str]] = field(default_factory=list)
    created_doctors: list[str] = field(default_factory=list)
    ambiguous_doctors: list[str] = field(default_factory=list)
    # Entries whose text was already imported from another version of the
    # document (a re-saved .docx has a new hash): likely duplicates.
    other_source_duplicates: list[int] = field(default_factory=list)


def existing_keys(db: DbSession, source_sha256: str) -> set[tuple[int, str]]:
    """``(entry_index, text_sha256)`` of the entries already imported from this file."""
    ref = LogEntry.origin_ref
    rows = db.execute(
        select(ref["entry_index"].as_integer(), ref["text_sha256"].as_string()).where(
            LogEntry.origin == ORIGIN, ref["source_sha256"].as_string() == source_sha256
        )
    )
    return {(index, text_hash) for index, text_hash in rows}


def texts_from_other_sources(db: DbSession, source_sha256: str) -> set[str]:
    ref = LogEntry.origin_ref
    rows = db.scalars(
        select(ref["text_sha256"].as_string()).where(
            LogEntry.origin == ORIGIN, ref["source_sha256"].as_string() != source_sha256
        )
    )
    return set(rows)


def _resolve_doctor(
    db: DbSession, name: str, *, user_id: int | None, report: LoadReport
) -> tuple[Doctor | None, bool]:
    """``(doctor, created)``; None when the name matches several doctors."""
    matches = match_doctors(db, name)
    if len(matches) == 1:
        return matches[0], False
    if matches:
        if name not in report.ambiguous_doctors:
            report.ambiguous_doctors.append(name)
        return None, False
    doctor = Doctor(name=name, aliases=[], created_by=user_id, updated_by=user_id)
    db.add(doctor)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity="doctor",
        entity_id=doctor.id,
        after=audit.snapshot(doctor),
    )
    return doctor, True


def _load_one(
    db: DbSession,
    item: dict[str, Any],
    *,
    source_sha256: str,
    model: str | None,
    patient: Patient,
    user_id: int | None,
    report: LoadReport,
) -> str | None:
    """Insert one staged entry; returns the created doctor's name, if any."""
    ext = item["extraction"]
    created_doctor = None
    doctor = None
    source_label = ext.get("source_label")
    if ext.get("doctor") and not normalize_name(ext["doctor"]):
        # Only a title ("Dra.") or punctuation: no doctor, keep the text.
        source_label = source_label or ext["doctor"][:200]
    elif ext.get("doctor"):
        doctor, created = _resolve_doctor(db, ext["doctor"], user_id=user_id, report=report)
        if created:
            created_doctor = doctor.name
        if doctor is None and not source_label:
            source_label = ext["doctor"][:200]  # ambiguous: keep the name visible
    data = {
        "patient_id": patient.id,
        "entry_date": date.fromisoformat(item["entry_date"]),
        "doctor_id": doctor.id if doctor else None,
        "source_label": source_label,
        "entry_type": ext["entry_type"],
        "channel": ext.get("channel"),
        "title": ext.get("title"),
        "indications": ext.get("indications"),
        "family_notes": ext.get("family_notes"),
        "review_status": REVIEW_PENDING,
    }
    origin_ref = {
        "source_sha256": source_sha256,
        "entry_index": item["entry_index"],
        "text_sha256": item["text_sha256"],
        "date_line": item["date_line"],
        "original_markdown": item["markdown"],
        "doctor_name": ext.get("doctor"),
        "next_appointment": ext.get("next_appointment"),
        "medication_events": ext.get("medication_events") or [],
        "extraction_model": model,
        "warnings": item.get("warnings") or [],
    }
    create_entry(db, data, user_id=user_id, origin=ORIGIN, origin_ref=origin_ref)
    return created_doctor


def load_staging(
    db: DbSession, staging: dict[str, Any], *, patient: Patient, user_id: int | None
) -> LoadReport:
    report = LoadReport()
    source_sha256 = staging["source_sha256"]
    done = existing_keys(db, source_sha256)
    elsewhere = texts_from_other_sources(db, source_sha256)
    for item in staging["entries"]:
        key = (item["entry_index"], item["text_sha256"])
        if key in done:
            report.skipped += 1
            continue
        if item["text_sha256"] in elsewhere:
            report.other_source_duplicates.append(item["entry_index"])
        if item.get("error") or not item.get("extraction"):
            report.failed += 1
            report.failures.append((item["entry_index"], item.get("error") or "no_extraction"))
            continue
        try:
            created = _load_one(
                db,
                item,
                source_sha256=source_sha256,
                model=staging.get("model"),
                patient=patient,
                user_id=user_id,
                report=report,
            )
            db.commit()
        except (LogEntryError, SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            db.rollback()
            report.failed += 1
            reason = str(exc) if isinstance(exc, LogEntryError) else type(exc).__name__
            report.failures.append((item["entry_index"], reason))
            continue
        done.add(key)
        report.new += 1
        if created:
            report.created_doctors.append(created)
    return report
