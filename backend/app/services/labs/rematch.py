"""Re-match the patient of lab reports that have none (#98, operator CLI
``python -m scripts.lab_reports rematch [--dry-run]``).

For every report with ``patient_id IS NULL`` it runs
``matching.identify_patient`` again on the name and ID number stored in
``raw_extraction`` (the PDF is never read or sent again; Claude, when that
step is reached, sees only the name, the ID and the patients' names). A
report a person assigned is never touched: only ``update_report`` assigns by
hand, and it never leaves the patient empty, so the ``patient_id IS NULL``
filter already excludes them.

An assigned report gets ``patient_id`` + ``patient_match``; its ``status``
becomes ``ok`` only by the rule extraction uses (``process.confident``: it
has results and no ambiguous value, and Claude did not choose the patient),
so a report also waiting on something else, or assigned by ``llm``, stays
«Por revisar»; a report someone already marked ``ok`` keeps it.
Each change is audited (``lab_report``, ``update``, no user) and committed on
its own. Running it again finds only what is still unassigned (idempotent).
``dry_run`` decides the same way (Claude included) and rolls back. Logs and
counts carry ids and codes, never names.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.lab import LabReport
from app.models.patient import Patient
from app.services import audit
from app.services.labs.matching import identify_patient
from app.services.labs.process import _text, confident
from app.services.labs.values import read_value

logger = logging.getLogger("app.labs")

ENTITY = "lab_report"


@dataclass
class RematchStats:
    candidates: int = 0
    already_assigned: int = 0
    assigned: Counter = field(default_factory=Counter)  # by method
    unassigned: Counter = field(default_factory=Counter)  # by reason
    skipped: Counter = field(default_factory=Counter)  # by reason
    marked_ok: int = 0


def _ambiguous(report: LabReport) -> bool:
    """A value extraction kept as text only because its reading stayed
    ambiguous (``process.save_report``'s rule, on the stored rows)."""
    return any(r.value_num is None and read_value(r.value_text).ambiguous for r in report.results)


def rematch(db: DbSession, *, ask=None, dry_run: bool = False) -> RematchStats:
    stats = RematchStats()
    stats.already_assigned = (
        db.scalar(select(func.count(LabReport.id)).where(LabReport.patient_id.is_not(None))) or 0
    )
    patients = list(db.scalars(select(Patient).order_by(Patient.id)))
    ids = list(
        db.scalars(
            select(LabReport.id).where(LabReport.patient_id.is_(None)).order_by(LabReport.id)
        )
    )
    db.rollback()
    stats.candidates = len(ids)
    for report_id in ids:
        # The row lock is held across the Claude call (seconds, one report at
        # a time, committed or rolled back right after): an edit of the same
        # report in the app waits for it instead of being overwritten.
        report = db.scalar(select(LabReport).where(LabReport.id == report_id).with_for_update())
        if report is None or report.patient_id is not None:
            stats.skipped["changed_meanwhile"] += 1
            db.rollback()
            continue
        data = report.raw_extraction
        if not isinstance(data, dict):
            stats.skipped["no_extraction"] += 1
            db.rollback()
            continue
        identified = identify_patient(
            patients,
            _text(data.get("patient_name"), 300),
            _text(data.get("patient_id_number"), 100),
            ask=ask,
        )
        match = identified.match
        if match.patient is None:
            stats.unassigned[identified.reason or "unknown"] += 1
            logger.info("lab rematch: report %s unassigned (%s)", report.id, identified.reason)
            db.rollback()
            continue
        stats.assigned[match.method] += 1
        becomes_ok = report.status == "needs_review" and confident(
            match.patient.id, match.method, report.results, _ambiguous(report)
        )
        stats.marked_ok += int(becomes_ok)
        logger.info("lab rematch: report %s assigned (%s)", report.id, match.method)
        if dry_run:
            db.rollback()
            continue
        before = audit.snapshot(report)
        report.patient_id = match.patient.id
        report.patient_match = match.method
        if becomes_ok:
            report.status = "ok"
        report.updated_at = func.now()
        db.flush()
        db.refresh(report)
        audit.record(
            db,
            user_id=None,
            action=audit.ACTION_UPDATE,
            entity=ENTITY,
            entity_id=report.id,
            before=before,
            after=audit.snapshot(report),
        )
        db.commit()
    return stats
