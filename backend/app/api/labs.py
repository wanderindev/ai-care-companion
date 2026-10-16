"""Labs API (DESIGN §6.5, §7): report list/detail/edit, manual upload,
analyte series, the analyte review and the pending lab portal links.

Every family member can edit a report and upload one (DESIGN §2: "everyone
can edit"), as with the bitácora; every edit is audited. Editing or merging
analytes (the shared catalog every trend depends on) is for admins.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_admin, require_user
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.attachment import Attachment
from app.models.job import Job
from app.models.lab import Analyte, LabLink, LabReport, LabResult
from app.models.patient import Patient
from app.models.user import User
from app.models.whatsapp import WhatsappMessage
from app.schemas.labs import (
    AnalyteListItem,
    AnalyteMerge,
    AnalyteOut,
    AnalyteSeries,
    AnalyteTriageAcceptAll,
    AnalyteTriageEstimate,
    AnalyteTriageOut,
    AnalyteTriageStarted,
    AnalyteUpdate,
    LabLinkOut,
    LabReportListItem,
    LabReportOut,
    LabReportPage,
    LabReportUpdate,
    LabResultOut,
    LabUploadIn,
    LabUploadJob,
    LabUploadOutcome,
    ReportStatus,
    SeriesPoint,
)
from app.schemas.log_entries import EntryAttachment
from app.services import audit, jobs
from app.services.labs import analytes as analyte_svc
from app.services.labs import process, triage
from app.services.labs import reports as svc

reports_router = APIRouter(prefix="/lab-reports", tags=["labs"])
analytes_router = APIRouter(prefix="/analytes", tags=["labs"])
links_router = APIRouter(prefix="/lab-links", tags=["labs"])

NOT_FOUND = "Informe de laboratorio no encontrado."
LINK_NOT_FOUND = "Enlace no encontrado."
LINK_RESOLVED = "Este enlace ya se resolvió."
LINK_IGNORED = "Este enlace se marcó como ignorado."


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _list_item(
    report: LabReport, summary: svc.ReportSummary, attachments: dict
) -> dict[str, object]:
    attachment = attachments.get(report.attachment_id)
    return {
        "id": report.id,
        "patient_id": report.patient_id,
        "collected_on": report.collected_on,
        "reported_on": report.reported_on,
        "report_date": svc.effective_date(report),
        "lab_name": report.lab_name,
        "source": report.source,
        "status": report.status,
        "patient_match": report.patient_match,
        "attachment": EntryAttachment.model_validate(attachment) if attachment else None,
        "result_count": summary.result_count,
        "out_of_range_count": summary.out_of_range_count,
        "flagged_names": summary.flagged_names,
        "panels": summary.panels,
        "created_at": report.created_at,
        "updated_at": report.updated_at,
    }


def _result(result: LabResult, analyte_names: dict[int, str]) -> LabResultOut:
    return LabResultOut(
        id=result.id,
        position=result.position,
        panel=result.panel,
        analyte_id=result.analyte_id,
        analyte_name=analyte_names.get(result.analyte_id) if result.analyte_id else None,
        raw_name=result.raw_name,
        value_text=result.value_text,
        value_num=_num(result.value_num),
        comparator=result.comparator,
        unit=result.unit,
        ref_low=_num(result.ref_low),
        ref_high=_num(result.ref_high),
        ref_text=result.ref_text,
        flag=result.flag,
    )


def _detail(db: DbSession, report: LabReport) -> LabReportOut:
    summary = svc.summaries(db, [report.id])[report.id]
    attachments = svc.attachments_by_id(db, [report.attachment_id])
    analyte_ids = {r.analyte_id for r in report.results if r.analyte_id}
    names = (
        {
            a.id: a.canonical_name
            for a in db.scalars(select(Analyte).where(Analyte.id.in_(analyte_ids)))
        }
        if analyte_ids
        else {}
    )
    files = list(attachments.values())
    files += db.scalars(
        select(Attachment)
        .where(
            Attachment.owner_type == "lab_report",
            Attachment.owner_id == report.id,
            Attachment.id != (report.attachment_id or 0),
        )
        .order_by(Attachment.id)
    ).all()
    return LabReportOut(
        **_list_item(report, summary, attachments),
        results=[_result(r, names) for r in report.results],
        attachments=[EntryAttachment.model_validate(a) for a in files],
    )


def _get_or_404(db: DbSession, report_id: int) -> LabReport:
    report = db.get(LabReport, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return report


@reports_router.get("", response_model=LabReportPage)
def list_lab_reports(
    patient_id: list[int] | None = Query(
        default=None, description="One or more patients (repeat the parameter for both)."
    ),
    include_unassigned: bool = Query(
        default=False,
        description="Also return reports whose patient was not identified (patient_id null).",
    ),
    status_: list[ReportStatus] | None = Query(
        default=None, alias="status", description="`needs_review` for «Por revisar»."
    ),
    date_from: date | None = Query(default=None, description="Inclusive, on the report date."),
    date_to: date | None = Query(default=None, description="Inclusive, on the report date."),
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Test name: a result's name as printed, or its analyte's name or "
        "aliases; substring, accent/case-insensitive.",
    ),
    out_of_range: bool = Query(
        default=False, description="Only reports with a result flagged alto/bajo/anormal."
    ),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> LabReportPage:
    """Reports, newest report date first (collected, else reported, else stored)."""
    conds = svc.filter_conditions(
        db,
        patient_ids=patient_id,
        include_unassigned=include_unassigned,
        statuses=status_,
        date_from=date_from,
        date_to=date_to,
        q=q,
        out_of_range=out_of_range,
    )
    reports, total = svc.list_reports(db, conds, limit=limit, offset=offset)
    summaries = svc.summaries(db, [r.id for r in reports])
    attachments = svc.attachments_by_id(db, [r.attachment_id for r in reports])
    return LabReportPage(
        items=[LabReportListItem(**_list_item(r, summaries[r.id], attachments)) for r in reports],
        total=total,
        limit=limit,
        offset=offset,
    )


@reports_router.post("/extract", response_model=LabUploadJob, status_code=status.HTTP_202_ACCEPTED)
def extract_lab_upload(
    payload: LabUploadIn,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user: User = Depends(require_user),
) -> LabUploadJob:
    """«Subir resultado»: queue one ``lab_extract`` job for files already
    stored with ``POST /attachments`` — one PDF, or photos (JPEG, PNG, WebP)
    of the pages of one report, in page order. ``patient_id`` is only a hint:
    it never overrides a document that names the other patient. Any family
    member may upload. Poll ``GET /jobs/{id}``, then
    ``GET /lab-reports/extract/{job_id}`` for the report. ``lab_link_id``:
    the files come from that lab link (report source ``link``); a pending
    or already fetched link is accepted, since a portal may hold several
    PDFs, each sent on its own request (#124); an ignored one is 409."""
    if len(set(payload.attachment_ids)) != len(payload.attachment_ids):
        raise HTTPException(status_code=400, detail="Hay archivos repetidos.")
    attachments = process.load_attachments(db, payload.attachment_ids)
    if attachments is None:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    if any(a.owner_type not in (None, "lab_report") for a in attachments):
        raise HTTPException(
            status_code=400,
            detail="Estos archivos ya pertenecen a otro registro. Súbelos de nuevo.",
        )
    problem = process.check_files(attachments)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if process.too_large(settings, attachments):
        raise HTTPException(
            status_code=413,
            detail="Los archivos son demasiado grandes para leerlos automáticamente.",
        )
    if payload.patient_id is not None and db.get(Patient, payload.patient_id) is None:
        raise HTTPException(status_code=400, detail="El paciente no existe.")
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="La lectura automática de laboratorios no está configurada.",
        )
    link = None
    if payload.lab_link_id is not None:
        link = db.get(LabLink, payload.lab_link_id, with_for_update=True)
        if link is None:
            raise HTTPException(status_code=404, detail=LINK_NOT_FOUND)
        if link.status == "ignored":
            raise HTTPException(status_code=409, detail=LINK_IGNORED)
    job = process.enqueue_upload(
        db,
        payload.attachment_ids,
        patient_id=payload.patient_id,
        lab_link_id=link.id if link is not None else None,
    )
    if link is not None and link.status == "pending":
        # A link may hold several PDFs (#124): each is its own request and
        # report; the link keeps the first file and job.
        _update_link(
            db,
            link,
            _user,
            status="fetched",
            reason=None,
            attachment_id=payload.attachment_ids[0],
            job_id=job.id,
        )
    db.commit()
    return LabUploadJob(job_id=job.id)


@reports_router.get("/extract/{job_id}", response_model=LabUploadOutcome)
def lab_upload_outcome(
    job_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> LabUploadOutcome:
    """The outcome of an upload job once it has finished (null before)."""
    job = db.get(Job, job_id)
    ids = job.payload.get("attachment_ids") if job is not None else None
    if job is None or job.kind != process.JOB_KIND or not isinstance(ids, list):
        raise HTTPException(status_code=404, detail="Tarea no encontrada.")
    extraction = None
    if job.status in ("done", "failed") and all(isinstance(i, int) for i in ids):
        attachments = process.load_attachments(db, ids)
        if attachments:
            extraction = process.find_extraction(db, attachments)
    return LabUploadOutcome(
        job_id=job.id,
        job_status=job.status,
        outcome=extraction.outcome if extraction else None,
        report_id=extraction.report_id if extraction else None,
    )


@reports_router.get("/{report_id}", response_model=LabReportOut)
def get_lab_report(
    report_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> LabReportOut:
    return _detail(db, _get_or_404(db, report_id))


@reports_router.patch("/{report_id}", response_model=LabReportOut)
def patch_lab_report(
    report_id: int,
    payload: LabReportUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> LabReportOut:
    """Correct the patient, dates or lab name, or mark the report reviewed
    (``{"status": "ok"}``, which needs a patient). A patient change records
    ``patient_match = "manual"``."""
    report = _get_or_404(db, report_id)
    changes = payload.model_dump(include=payload.model_fields_set)
    try:
        svc.update_report(db, report, changes, user_id=user.id)
    except svc.LabReportError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    db.commit()
    db.refresh(report)
    return _detail(db, report)


@reports_router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lab_report(
    report_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> None:
    """Admins only (#115): hard-delete a report that belongs to neither
    patient. Results go with it, its files are unlinked (kept), the file is
    never extracted again, and analytes left with no result while still under
    review are deleted. See ``app.services.labs.reports``."""
    try:
        svc.delete_report(db, report_id, user_id=user.id)
    except svc.ReportNotFound:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND) from None
    db.commit()


@analytes_router.get("/{analyte_id}/series", response_model=AnalyteSeries)
def analyte_series(
    analyte_id: int,
    patient: int = Query(description="The patient's id."),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> AnalyteSeries:
    """One patient's results for one analyte over time, oldest first. Each
    point keeps its own unit and reference range (they can change between
    labs); ``value_num`` is null when the value is not a number."""
    analyte = db.get(Analyte, analyte_id)
    if analyte is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Examen no encontrado.")
    if db.get(Patient, patient) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente no encontrado.")
    points = [
        SeriesPoint(
            result_id=result.id,
            report_id=report.id,
            date=svc.effective_date(report),
            collected_on=report.collected_on,
            reported_on=report.reported_on,
            lab_name=report.lab_name,
            report_status=report.status,
            raw_name=result.raw_name,
            value_text=result.value_text,
            value_num=_num(result.value_num),
            comparator=result.comparator,
            unit=result.unit,
            ref_low=_num(result.ref_low),
            ref_high=_num(result.ref_high),
            ref_text=result.ref_text,
            flag=result.flag,
        )
        for result, report in svc.series(db, analyte_id, patient)
    ]
    return AnalyteSeries(
        analyte=AnalyteOut.model_validate(analyte), patient_id=patient, points=points
    )


# ── Analyte review (#15) ─────────────────────────────────────────────────────


def _triage_out(analyte: Analyte, names: dict[int, str]) -> AnalyteTriageOut | None:
    data = analyte.triage
    if not data or data.get("status") not in ("pending", "rejected"):
        return None  # applied / accepted: nothing left to show
    try:
        return AnalyteTriageOut(
            **{k: data.get(k) for k in AnalyteTriageOut.model_fields if k != "target_name"},
            target_name=names.get(data.get("target_id")),
        )
    except ValueError:
        return None


def _analyte_items(db: DbSession, analytes: list[Analyte]) -> list[AnalyteListItem]:
    stats = analyte_svc.stats(db, [a.id for a in analytes])
    target_ids = {a.triage.get("target_id") for a in analytes if a.triage} - {None}
    names = (
        dict(
            db.execute(
                select(Analyte.id, Analyte.canonical_name).where(Analyte.id.in_(target_ids))
            ).all()
        )
        if target_ids
        else {}
    )
    return [
        AnalyteListItem(
            **AnalyteOut.model_validate(a).model_dump(),
            result_count=stats[a.id].result_count,
            examples=stats[a.id].examples,
            triage=_triage_out(a, names),
        )
        for a in analytes
    ]


def _analyte_or_404(db: DbSession, analyte_id: int) -> Analyte:
    analyte = db.get(Analyte, analyte_id)
    if analyte is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Examen no encontrado.")
    return analyte


@analytes_router.get("", response_model=list[AnalyteListItem])
def list_analytes(
    needs_review: bool | None = Query(
        default=None, description="`true`: only the analytes flagged for review."
    ),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> list[AnalyteListItem]:
    """The analyte catalog by name, with each one's result count and up to
    three example results (printed name, value, unit — no patient)."""
    return _analyte_items(db, analyte_svc.list_analytes(db, needs_review=needs_review))


@analytes_router.get("/triage/estimate", response_model=AnalyteTriageEstimate)
def triage_estimate(
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user: User = Depends(require_admin),
) -> AnalyteTriageEstimate:
    """Admins only (#100): how many analytes a triage run would send to
    Claude, in how many calls, and a rough cost; plus the proposals waiting
    and the run in progress, if any."""
    est = triage.estimate(db, settings)
    job = triage.triage_job_active(db)
    return AnalyteTriageEstimate(
        analytes=est.analytes,
        batches=est.batches,
        model=est.model,
        cost_usd=est.cost_usd,
        pending_proposals=len(triage.pending_proposals(db)),
        running_job_id=job.id if job is not None else None,
    )


@analytes_router.post(
    "/triage", response_model=AnalyteTriageStarted, status_code=status.HTTP_202_ACCEPTED
)
def start_triage(
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_admin),
) -> AnalyteTriageStarted:
    """Admins only: queue the ``analyte_triage`` job (proposals only — nothing
    is applied until accepted on the page). 409 while one is queued/running
    or when nothing is left to triage; 503 without a Claude key."""
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="La revisión con IA no está configurada.")
    if triage.triage_job_active(db) is not None:
        raise HTTPException(status_code=409, detail="Ya hay una revisión con IA en curso.")
    if triage.pending_count(db) == 0:
        raise HTTPException(status_code=409, detail="No hay exámenes nuevos para revisar con IA.")
    job = jobs.enqueue(db, triage.JOB_KIND, {"user_id": user.id}, message="Revisión con IA en cola")
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_CREATE,
        entity="job",
        entity_id=job.id,
        after={"kind": triage.JOB_KIND},
    )
    db.commit()
    return AnalyteTriageStarted(job_id=job.id)


@analytes_router.post("/triage/accept-all", response_model=AnalyteTriageAcceptAll)
def accept_all_triage(
    db: DbSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AnalyteTriageAcceptAll:
    """Admins only: «Aceptar todas las propuestas» — apply every pending
    merge / keep proposal through the review services (each audited). A
    refused one (different specimens, name taken) stays pending."""
    counts = triage.accept_all(db, user_id=user.id)
    db.commit()
    return AnalyteTriageAcceptAll(**counts)


def _triage_action(db: DbSession, analyte_id: int, fn, user: User) -> AnalyteListItem:
    analyte = db.get(Analyte, analyte_id, with_for_update=True)
    if analyte is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Examen no encontrado.")
    try:
        result = fn(db, analyte, user_id=user.id)
    except analyte_svc.AnalyteError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    db.commit()
    db.refresh(result)
    return _analyte_items(db, [result])[0]


@analytes_router.post("/{analyte_id}/triage/accept", response_model=AnalyteListItem)
def accept_triage(
    analyte_id: int, db: DbSession = Depends(get_db), user: User = Depends(require_admin)
) -> AnalyteListItem:
    """Admins only: apply this analyte's proposal (merge → the target is
    returned; keep → the confirmed analyte). Same rules as the manual
    buttons: different specimens or a taken name answer 409."""
    return _triage_action(db, analyte_id, triage.apply_proposal, user)


@analytes_router.post("/{analyte_id}/triage/reject", response_model=AnalyteListItem)
def reject_triage(
    analyte_id: int, db: DbSession = Depends(get_db), user: User = Depends(require_admin)
) -> AnalyteListItem:
    """Admins only: dismiss this analyte's proposal (it stays under review
    for the manual buttons, and is not sent to Claude again). Audited."""
    return _triage_action(db, analyte_id, triage.reject_proposal, user)


@analytes_router.patch("/{analyte_id}", response_model=AnalyteListItem)
def patch_analyte(
    analyte_id: int,
    payload: AnalyteUpdate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AnalyteListItem:
    """Admins only: confirm (``needs_review: false``) and/or fix the canonical
    name, unit, category or specimen. A rename keeps the old name as an
    alias; 409 when the name belongs to another analyte (merge instead)."""
    analyte = _analyte_or_404(db, analyte_id)
    changes = payload.model_dump(include=payload.model_fields_set)
    try:
        analyte_svc.update_analyte(db, analyte, changes, user_id=user.id)
    except analyte_svc.AnalyteError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    db.commit()
    db.refresh(analyte)
    return _analyte_items(db, [analyte])[0]


@analytes_router.post("/{analyte_id}/merge", response_model=AnalyteListItem)
def merge_analyte(
    analyte_id: int,
    payload: AnalyteMerge,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AnalyteListItem:
    """Admins only: fold the analyte ``source_id`` into this one — its results
    move here, its name and aliases become aliases, and it is deleted. 409
    when the specimens differ, unless ``allow_specimen_mismatch``."""
    try:
        target = analyte_svc.merge_analytes(
            db,
            target_id=analyte_id,
            source_id=payload.source_id,
            user_id=user.id,
            allow_specimen_mismatch=payload.allow_specimen_mismatch,
        )
    except analyte_svc.AnalyteError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    db.commit()
    db.refresh(target)
    return _analyte_items(db, [target])[0]


# ── Lab portal links (#16) ───────────────────────────────────────────────────


def _update_link(db: DbSession, link: LabLink, user: User, **values) -> None:
    before = audit.snapshot(link)
    for key, value in values.items():
        setattr(link, key, value)
    link.updated_by = user.id
    db.flush()
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_UPDATE,
        entity="lab_link",
        entity_id=link.id,
        before=before,
        after=audit.snapshot(link),
    )


def _link_out(link: LabLink, message: WhatsappMessage | None) -> LabLinkOut:
    return LabLinkOut(
        id=link.id,
        url=link.url,
        host=link.host,
        status=link.status,
        reason=link.reason,
        patient_id=link.patient_id,
        sent_at=message.sent_at if message is not None else None,
        sender=message.sender if message is not None else None,
        report_id=link.report_id,
        created_at=link.created_at,
    )


@links_router.get("", response_model=list[LabLinkOut])
def list_lab_links(
    status_filter: Literal["pending", "fetched", "failed", "ignored"] = Query(
        "pending", alias="status"
    ),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
) -> list[LabLinkOut]:
    """Lab portal links found in the chat (default: the pending ones, newest
    message first; at most 200). Every patient's links: the patient is only a
    guess, so the client decides what to show."""
    rows = db.execute(
        select(LabLink, WhatsappMessage)
        .outerjoin(WhatsappMessage, WhatsappMessage.id == LabLink.message_id)
        .where(LabLink.status == status_filter)
        .order_by(WhatsappMessage.sent_at.desc().nulls_last(), LabLink.id.desc())
        .limit(200)
    ).all()
    return [_link_out(link, message) for link, message in rows]


@links_router.post("/{link_id}/ignore", response_model=LabLinkOut)
def ignore_lab_link(
    link_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
) -> LabLinkOut:
    """«Ignorar»: a pending link that is not a lab result (or not needed)
    leaves the list. Any family member may do it; audited."""
    link = db.get(LabLink, link_id, with_for_update=True)
    if link is None:
        raise HTTPException(status_code=404, detail=LINK_NOT_FOUND)
    if link.status == "fetched":
        raise HTTPException(status_code=409, detail=LINK_RESOLVED)
    if link.status != "ignored":
        _update_link(db, link, user, status="ignored")
        db.commit()
    message = db.get(WhatsappMessage, link.message_id) if link.message_id else None
    return _link_out(link, message)
