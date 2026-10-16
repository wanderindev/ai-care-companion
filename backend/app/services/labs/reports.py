"""Lab reports for the Labs screens (DESIGN §6.5): list filters, edits, series.

Listing: reports sort by their *report date* — ``collected_on``, else
``reported_on``, else the day the report was stored — newest first. The
``patient_ids`` filter is strict; ``include_unassigned`` adds the reports
whose patient was not identified (``patient_id`` NULL), so the Labs tab can
show them under every patient selection until someone assigns them.

Test-name search (``q``) matches a report when one of its results matches by
raw name, or by its analyte's canonical name or aliases: substring, accent
and case insensitive (``immutable_unaccent`` on both sides). With
``out_of_range`` too, the *same* result must match both ("Glucosa fuera de
rango", not "a report with Glucosa and something else out of range").

Edits (``update_report``): patient, dates, lab name and status. A patient
change sets ``patient_match = "manual"``; ``status = "ok"`` (reviewed) needs a
patient. Audited with before/after in the caller's transaction.

Delete (``delete_report``, admins, #115): a hard delete for a report that
belongs to neither patient. Results go with it (``ON DELETE CASCADE``); its
page files are unlinked, never deleted (the WhatsApp message keeps its file).
The ``lab_extractions`` row stays (``report_id`` → NULL) with its
``lab_report`` outcome, so the same file is never extracted again; a lab link
that fetched it stays ``fetched``. Analytes the report's results pointed to
that are still ``needs_review`` and have no result left were created by this
report alone: they are deleted too (audited), so «Revisar exámenes» and the
analyte triage never see an empty name. Reviewed analytes are never touched,
and a triage proposal only ever targets a reviewed analyte. The audit row
keeps ids, dates, lab name, patient id and counts — no result values.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, cast, exists, func, or_, select
from sqlalchemy import Date as SqlDate
from sqlalchemy.orm import Session as DbSession

from app.models.attachment import Attachment
from app.models.lab import Analyte, LabReport, LabResult
from app.models.patient import Patient
from app.services import audit
from app.services.attachments import unlink_attachments
from app.services.labs.analytes import ENTITY as ANALYTE_ENTITY

ENTITY = "lab_report"
OUT_OF_RANGE_FLAGS = ("alto", "bajo", "anormal")
FLAGGED_NAMES_MAX = 6
EDITABLE_FIELDS = frozenset({"patient_id", "collected_on", "reported_on", "lab_name", "status"})
MIN_DATE = date(1900, 1, 1)
# Reports describe past samples; one day of slack for time zones.
FUTURE_SKEW = timedelta(days=1)

report_date = func.coalesce(
    LabReport.collected_on, LabReport.reported_on, cast(LabReport.created_at, SqlDate)
)


class LabReportError(ValueError):
    """Invalid edit; the message is Spanish and safe to show."""


# ── Listing ──────────────────────────────────────────────────────────────────


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _name_matches(db: DbSession, q: str) -> ColumnElement[bool] | None:
    needle = (q or "").strip()
    if not needle:
        return None
    # Unaccent and lowercase in the database, the same function as the columns.
    folded = db.scalar(select(func.lower(func.immutable_unaccent(needle))))
    pattern = _like_pattern(folded)

    def fold(column):
        return func.lower(func.immutable_unaccent(column))

    analyte_match = (
        select(Analyte.id)
        .where(
            Analyte.id == LabResult.analyte_id,
            or_(
                fold(Analyte.canonical_name).like(pattern, escape="\\"),
                fold(func.array_to_string(Analyte.aliases, " | ")).like(pattern, escape="\\"),
            ),
        )
        .correlate(LabResult)
        .exists()
    )
    return or_(fold(LabResult.raw_name).like(pattern, escape="\\"), analyte_match)


def filter_conditions(
    db: DbSession,
    *,
    patient_ids: Sequence[int] | None = None,
    include_unassigned: bool = False,
    statuses: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    out_of_range: bool = False,
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if patient_ids:
        scope = LabReport.patient_id.in_(patient_ids)
        conds.append(or_(scope, LabReport.patient_id.is_(None)) if include_unassigned else scope)
    if statuses:
        conds.append(LabReport.status.in_(statuses))
    if date_from:
        conds.append(report_date >= date_from)
    if date_to:
        conds.append(report_date <= date_to)
    result_conds: list[ColumnElement[bool]] = []
    name = _name_matches(db, q) if q else None
    if name is not None:
        result_conds.append(name)
    if out_of_range:
        result_conds.append(LabResult.flag.in_(OUT_OF_RANGE_FLAGS))
    if result_conds:
        conds.append(
            exists().where(LabResult.report_id == LabReport.id, *result_conds).correlate(LabReport)
        )
    return conds


def list_reports(
    db: DbSession,
    conds: list[ColumnElement[bool]],
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[LabReport], int]:
    where = and_(*conds) if conds else None
    count_stmt = select(func.count()).select_from(LabReport)
    stmt = select(LabReport)
    if where is not None:
        count_stmt = count_stmt.where(where)
        stmt = stmt.where(where)
    total = db.scalar(count_stmt) or 0
    stmt = (
        stmt.order_by(report_date.desc(), LabReport.created_at.desc(), LabReport.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt)), total


@dataclass
class ReportSummary:
    result_count: int = 0
    out_of_range_count: int = 0
    flagged_names: list[str] | None = None
    panels: list[str] | None = None


def summaries(db: DbSession, report_ids: Iterable[int]) -> dict[int, ReportSummary]:
    """Counts, flagged names and panels per report, in one query."""
    ids = list(report_ids)
    out = {i: ReportSummary(flagged_names=[], panels=[]) for i in ids}
    if not ids:
        return out
    rows = db.execute(
        select(LabResult.report_id, LabResult.raw_name, LabResult.flag, LabResult.panel)
        .where(LabResult.report_id.in_(ids))
        .order_by(LabResult.report_id, LabResult.position, LabResult.id)
    )
    for report_id, raw_name, flag, panel in rows:
        summary = out[report_id]
        summary.result_count += 1
        if flag in OUT_OF_RANGE_FLAGS:
            summary.out_of_range_count += 1
            if len(summary.flagged_names) < FLAGGED_NAMES_MAX:
                summary.flagged_names.append(raw_name)
        if panel and panel not in summary.panels:
            summary.panels.append(panel)
    return out


def attachments_by_id(db: DbSession, ids: Iterable[int | None]) -> dict[int, Attachment]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {a.id: a for a in db.scalars(select(Attachment).where(Attachment.id.in_(wanted)))}


def effective_date(report: LabReport) -> date:
    return report.collected_on or report.reported_on or report.created_at.date()


# ── Edits ────────────────────────────────────────────────────────────────────


def _check_date(value: date | None) -> None:
    if value is not None and (value < MIN_DATE or value > date.today() + FUTURE_SKEW):
        raise LabReportError("La fecha no es válida.")


def update_report(
    db: DbSession, report: LabReport, changes: dict[str, Any], *, user_id: int | None
) -> LabReport:
    """Apply ``changes`` (fields from ``EDITABLE_FIELDS``), validate and audit.
    Raises ``LabReportError``; the caller rolls back and commits."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise LabReportError(f"Campos no permitidos: {', '.join(sorted(unknown))}.")
    before = audit.snapshot(report)
    if "patient_id" in changes:
        patient_id = changes["patient_id"]
        if patient_id is None or db.get(Patient, patient_id) is None:
            raise LabReportError("El paciente no existe.")
        if patient_id != report.patient_id:
            report.patient_match = "manual"
    for field, value in changes.items():
        setattr(report, field, value)
    _check_date(report.collected_on)
    _check_date(report.reported_on)
    if report.status == "ok" and report.patient_id is None:
        raise LabReportError("Indica el paciente antes de marcar el informe como revisado.")
    after = audit.snapshot(report)
    if after != before:
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_UPDATE,
            entity=ENTITY,
            entity_id=report.id,
            before=before,
            after=after,
        )
        report.updated_by = user_id
        report.updated_at = func.now()
    db.flush()
    return report


# ── Delete ───────────────────────────────────────────────────────────────────


class ReportNotFound(LookupError):
    pass


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _delete_orphan_analytes(
    db: DbSession, analyte_ids: list[int], *, report_id: int, user_id: int | None
) -> list[int]:
    """Delete the analytes among ``analyte_ids`` still ``needs_review`` with
    no result left (run after the report's results are gone). Audited."""
    if not analyte_ids:
        return []
    # FOR UPDATE: a result being stored meanwhile (FOR KEY SHARE on its
    # analyte) waits for this transaction, or this waits for it.
    candidates = list(
        db.scalars(
            select(Analyte)
            .where(Analyte.id.in_(analyte_ids), Analyte.needs_review.is_(True))
            .order_by(Analyte.id)
            .with_for_update()
        )
    )
    if not candidates:
        return []
    used = set(
        db.scalars(
            select(LabResult.analyte_id)
            .where(LabResult.analyte_id.in_([a.id for a in candidates]))
            .distinct()
        )
    )
    deleted: list[int] = []
    for analyte in candidates:
        if analyte.id in used:
            continue
        before = audit.snapshot(analyte, bookkeeping=True)
        deleted.append(analyte.id)
        db.delete(analyte)
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_DELETE,
            entity=ANALYTE_ENTITY,
            entity_id=analyte.id,
            before=before,
            after={"orphaned_by_lab_report": report_id},
        )
    db.flush()
    return deleted


def delete_report(db: DbSession, report_id: int, *, user_id: int | None) -> dict[str, Any]:
    """Hard-delete a report (see the module docstring). Returns the audit
    summary; raises ``ReportNotFound``. The caller commits."""
    report = db.scalar(select(LabReport).where(LabReport.id == report_id).with_for_update())
    if report is None:
        raise ReportNotFound(report_id)
    analyte_ids = sorted(
        set(
            db.scalars(
                select(LabResult.analyte_id).where(
                    LabResult.report_id == report_id, LabResult.analyte_id.is_not(None)
                )
            )
        )
    )
    result_count = db.scalar(
        select(func.count()).select_from(LabResult).where(LabResult.report_id == report_id)
    )
    unlinked = unlink_attachments(db, owner_type=ENTITY, owner_id=report_id, user_id=user_id)
    summary: dict[str, Any] = {
        "id": report.id,
        "patient_id": report.patient_id,
        "patient_match": report.patient_match,
        "collected_on": _iso(report.collected_on),
        "reported_on": _iso(report.reported_on),
        "lab_name": report.lab_name,
        "source": report.source,
        "status": report.status,
        "attachment_id": report.attachment_id,
        "source_message_id": report.source_message_id,
        "created_at": _iso(report.created_at),
        "created_by": report.created_by,
        "result_count": result_count or 0,
        "unlinked_attachment_ids": [a.id for a in unlinked],
    }
    db.delete(report)
    db.flush()  # the results go with it (ON DELETE CASCADE)
    summary["deleted_analyte_ids"] = _delete_orphan_analytes(
        db, analyte_ids, report_id=report_id, user_id=user_id
    )
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_DELETE,
        entity=ENTITY,
        entity_id=report_id,
        before=summary,
    )
    return summary


# ── Analyte series ───────────────────────────────────────────────────────────


def series(db: DbSession, analyte_id: int, patient_id: int) -> list[tuple[LabResult, LabReport]]:
    """Every result of one analyte for one patient, oldest first (report date,
    then report, then the result's position)."""
    stmt = (
        select(LabResult, LabReport)
        .join(LabReport, LabReport.id == LabResult.report_id)
        .where(LabResult.analyte_id == analyte_id, LabReport.patient_id == patient_id)
        .order_by(report_date.asc(), LabReport.id.asc(), LabResult.position.asc(), LabResult.id)
    )
    return [(row[0], row[1]) for row in db.execute(stmt)]
