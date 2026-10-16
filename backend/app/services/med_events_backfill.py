"""Backfill the derived medication history from imported bitácora entries
(operator CLI: ``python -m scripts.med_events backfill``).

The DOCX importer keeps each entry's medication events in
``log_entries.origin_ref.medication_events`` (a list of ``{kind, medication,
detail}``, see ``bitacora_import.extract``). For every imported entry
(``origin = 'import_docx'``) this creates one ``med_events`` row per event:

- patient: the entry's; ``event_date``: the entry's date (the extraction has
  no date of its own per event);
- ``kind`` mapped from the extractor's English values (:data:`KIND_MAP`):
  ``start`` → ``inicio``, ``stop`` → ``suspension``, ``change`` →
  ``cambio_dosis``, ``substitution`` → ``sustitucion``. Anything else is
  skipped and counted (``unknown_kind``): guessing a kind would put a wrong
  fact in front of the family;
- ``medication`` and ``detail`` as extracted (trimmed; blank detail → null).
  An event without a medication, or one longer than the column (200), is
  skipped and counted (``invalid``), never truncated;
- ``source_entry_id`` = the entry, ``source_index`` = the event's position
  in the list, ``review_status = pending_review``.

Idempotent: ``(source_entry_id, source_index)`` is unique (migration 0014),
so events already created are skipped (``existing``), whatever the family
did to them since (confirmed or edited). An event **discarded** in the app
(hard delete; its audit ``delete`` row names the entry and index) is never
recreated (``deleted_before``).

Counts only: nothing here logs or prints medical text.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.audit import AuditLog
from app.models.log_entry import LogEntry
from app.models.medication import REVIEW_PENDING, MedEvent
from app.services import meds

ORIGIN = "import_docx"
KIND_MAP = {
    "start": "inicio",
    "stop": "suspension",
    "change": "cambio_dosis",
    "substitution": "sustitucion",
}
MEDICATION_MAX = 200


@dataclass
class BackfillReport:
    entries: int = 0  # imported entries with at least one medication event
    events: int = 0  # events found in those entries
    existing: int = 0  # already created by an earlier run
    deleted_before: int = 0  # discarded in the app: not recreated
    unknown_kind: int = 0
    invalid: int = 0  # no medication, or too long for the column
    created: int = 0


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def discarded_sources(db: DbSession) -> set[tuple[int, int]]:
    """``(source_entry_id, source_index)`` of every derived event deleted in
    the app, from the audit log."""
    keys: set[tuple[int, int]] = set()
    for before in db.scalars(
        select(AuditLog.before).where(
            AuditLog.entity == meds.EVENT_ENTITY, AuditLog.action == "delete"
        )
    ):
        if not isinstance(before, dict):
            continue
        entry_id, index = before.get("source_entry_id"), before.get("source_index")
        if isinstance(entry_id, int) and isinstance(index, int):
            keys.add((entry_id, index))
    return keys


def backfill(db: DbSession, *, user_id: int | None) -> BackfillReport:
    """Create the missing events in the caller's transaction (the caller
    commits, or rolls back for a dry run)."""
    report = BackfillReport()
    existing = {
        (entry_id, index)
        for entry_id, index in db.execute(
            select(MedEvent.source_entry_id, MedEvent.source_index).where(
                MedEvent.source_entry_id.is_not(None), MedEvent.source_index.is_not(None)
            )
        )
    }
    discarded = discarded_sources(db)
    entries = db.scalars(
        select(LogEntry)
        .where(LogEntry.origin == ORIGIN, LogEntry.origin_ref.has_key("medication_events"))
        .order_by(LogEntry.entry_date, LogEntry.id)
    ).all()
    for entry in entries:
        events = (entry.origin_ref or {}).get("medication_events")
        if not isinstance(events, list) or not events:
            continue
        report.entries += 1
        for index, event in enumerate(events):
            report.events += 1
            key = (entry.id, index)
            if key in existing:
                report.existing += 1
                continue
            if key in discarded:
                report.deleted_before += 1
                continue
            if not isinstance(event, dict):
                report.invalid += 1
                continue
            kind = KIND_MAP.get(event.get("kind"))
            if kind is None:
                report.unknown_kind += 1
                continue
            medication = _text(event.get("medication"))
            if medication is None or len(medication) > MEDICATION_MAX:
                report.invalid += 1
                continue
            meds.create_event(
                db,
                {
                    "patient_id": entry.patient_id,
                    "event_date": entry.entry_date,
                    "kind": kind,
                    "medication": medication,
                    "detail": _text(event.get("detail")),
                    "source_entry_id": entry.id,
                    "source_index": index,
                    "review_status": REVIEW_PENDING,
                },
                user_id=user_id,
            )
            existing.add(key)
            report.created += 1
    return report
