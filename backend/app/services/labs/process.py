"""Lab extraction for stored attachments (DESIGN §6.5).

Two entry points, both run by the ``lab_extract`` job:

- ``process_attachment``: one PDF (every new PDF of a ``labs_only`` WhatsApp
  import; in ``daily``/``backfill`` imports, the images and PDFs the media
  classifier (#37) called lab reports — a photo is read like a one-page
  upload, but the report keeps its WhatsApp source message).
- ``process_upload``: a manual upload (#15) — one PDF, or one or more photos
  (JPEG, PNG, WebP) of the pages of one report, in page order, with an
  optional patient hint (``matching.apply_patient_hint``).

The **extraction key** (``extraction_key``) identifies the content: a single
file's SHA-256, or, for several photos, the SHA-256 of their ordered SHA-256s
(``"lab-pages:" + ",".join(shas)``) — the same pages in the same order are
the same document. It is stored in ``lab_extractions.sha256``. Steps:

1. Take a transaction-scoped advisory lock on the key, so two jobs for the
   same content never both call Claude.
2. A ``lab_extractions`` row for that key means the content was already
   processed (lab report, not a lab report, or a permanent failure): return
   ``duplicate`` without calling Claude. Same content, never twice.
3. Over the caps → recorded as ``failed`` / ``too_large``: a PDF over
   ``LAB_PDF_MAX_MB``; photos over ``extract.MAX_IMAGES`` pages, over
   ``extract.IMAGE_MAX_BYTES`` each or over ``LAB_PDF_MAX_MB`` together.
4. Read the file(s) from storage (streamed, capped), send them to Claude
   (``extract.extract_pdf`` / ``extract.extract_images``).
5. Not a lab report → recorded ``not_lab_report``. A lab report → a
   ``lab_reports`` row (patient matched by CIP, then name, name variant and
   Claude — ``matching.identify_patient``; then the hint),
   one ``lab_results`` row per result (analyte matched by alias; unknown
   names create analytes flagged ``needs_review``), audited with no user,
   the files linked to the report, and recorded ``lab_report``.

Transient API problems (429, 5xx, connection) raise ``JobError`` and record
nothing, so the job's backoff retries; key/model problems (401/403/404) and a
missing key raise ``PermanentJobError`` and record nothing either (fix the
setting, re-run). File problems (400/413, refusal, truncated or invalid
output, too large) are recorded as ``failed`` and are never retried.

Commits nothing itself: the caller (the job) commits. Logs carry only ids,
outcome codes and token counts — never names, values or document text.
"""

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings
from app.models.attachment import Attachment
from app.models.job import Job
from app.models.lab import FLAGS, LabExtraction, LabReport, LabResult
from app.models.patient import Patient
from app.models.whatsapp import WhatsappMessage
from app.services import audit, jobs
from app.services.attachments import PDF, link_attachments
from app.services.labs import extract, patient_llm
from app.services.labs.matching import AnalyteMatcher, apply_patient_hint, identify_patient
from app.services.labs.values import (
    compute_flag,
    decimal_convention,
    parse_range,
    read_value,
    resolve_value,
)
from app.services.storage import Storage

logger = logging.getLogger("app.labs")

JOB_KIND = "lab_extract"
MB = 1024 * 1024

Progress = Callable[[int, str | None], None]


@dataclass(frozen=True)
class Outcome:
    status: str  # "lab_report" | "not_lab_report" | "failed" | "duplicate"
    report_id: int | None = None
    error: str | None = None


def enqueue_extraction(
    db: DbSession,
    attachment_id: int,
    *,
    source_message_id: int | None = None,
    lab_link_id: int | None = None,
) -> Job:
    """Queue a ``lab_extract`` job for one attachment (caller commits).
    ``lab_link_id``: the PDF was downloaded from that lab link (source
    ``link``; the link gets the report)."""
    payload: dict[str, Any] = {
        "attachment_id": attachment_id,
        "source_message_id": source_message_id,
    }
    if lab_link_id is not None:
        payload["lab_link_id"] = lab_link_id
    return jobs.enqueue(db, JOB_KIND, payload, message="En cola")


def enqueue_upload(
    db: DbSession,
    attachment_ids: list[int],
    *,
    patient_id: int | None,
    lab_link_id: int | None = None,
) -> Job:
    """Queue a ``lab_extract`` job for a manual upload (caller commits).
    Payload: ids only — the files in page order, the patient hint and, when
    the files were downloaded by hand from a lab link, that link."""
    payload: dict[str, Any] = {"attachment_ids": list(attachment_ids), "patient_id": patient_id}
    if lab_link_id is not None:
        payload["lab_link_id"] = lab_link_id
    return jobs.enqueue(db, JOB_KIND, payload, message="En cola")


def extraction_key(attachments: list[Attachment]) -> str:
    """One file: its SHA-256. Several pages: the SHA-256 of their ordered SHA-256s."""
    if len(attachments) == 1:
        return attachments[0].sha256
    joined = "lab-pages:" + ",".join(a.sha256 for a in attachments)
    return hashlib.sha256(joined.encode("ascii")).hexdigest()


def find_extraction(db: DbSession, attachments: list[Attachment]) -> LabExtraction | None:
    if not attachments:
        return None
    key = extraction_key(attachments)
    return db.scalar(select(LabExtraction).where(LabExtraction.sha256 == key))


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _read_object(storage: Storage, key: str, cap: int) -> bytearray | None:
    """The object's bytes (one buffer, grown in place), or None when it turns
    out larger than ``cap``."""
    data = bytearray()
    for chunk in storage.iter_object(key):
        if len(data) + len(chunk) > cap:
            return None
        data += chunk
    return data


def _record(
    db: DbSession,
    key: str,
    attachment: Attachment,
    outcome: str,
    *,
    error: str | None = None,
    report: LabReport | None = None,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
) -> Outcome:
    db.add(
        LabExtraction(
            sha256=key,
            attachment_id=attachment.id,
            report_id=report.id if report is not None else None,
            outcome=outcome,
            error=error,
            model=model,
            usage=usage or {},
        )
    )
    db.flush()
    return Outcome(outcome, report.id if report is not None else None, error)


def _usage_summary(result: extract.Extraction, model: str) -> dict[str, Any]:
    summary: dict[str, Any] = result.usage.as_dict()
    cost = result.usage.cost_usd(model)
    summary["cost_usd"] = round(cost, 6) if cost is not None else None
    return summary


def confident(
    patient_id: int | None, patient_match: str | None, results: Sequence[Any], ambiguous: bool
) -> bool:
    """A report is ``ok`` (not «Por revisar») when its patient was identified
    by the document's own rules (not by the uploader's hint alone, ``manual``,
    nor by Claude, ``llm``: a person confirms those), it has results and no
    value stayed ambiguous. ``rematch`` uses the same rule."""
    return bool(
        patient_id is not None
        and patient_match not in ("manual", "llm")
        and results
        and not ambiguous
    )


def save_report(
    db: DbSession,
    data: dict[str, Any],
    *,
    attachment: Attachment | None,
    source: str,
    source_message_id: int | None,
    extra_attachments: Sequence[Attachment] = (),
    patient_hint_id: int | None = None,
    ask: patient_llm.Asker | None = None,
) -> LabReport:
    """Store an extracted lab report and its results (caller commits).
    ``attachment`` is the report's file (the first page of photos);
    ``extra_attachments`` are the other pages, linked to the report too.
    ``patient_hint_id``: see ``matching.apply_patient_hint``. ``ask``: Claude
    as the last patient-matching step (``matching.identify_patient``)."""
    patients = list(db.scalars(select(Patient).order_by(Patient.id)))
    name, id_number = (
        _text(data.get("patient_name"), 300),
        _text(data.get("patient_id_number"), 100),
    )
    identified = identify_patient(patients, name, id_number, ask=ask)
    match = identified.match
    logger.info("lab extraction: patient %s", match.method or identified.reason)
    hinted = next((p for p in patients if p.id == patient_hint_id), None)
    if hinted is not None:
        hint = apply_patient_hint(patients, hinted, match, name, id_number)
        if hint.conflict:
            logger.info("lab extraction: patient hint contradicted by the document")
        match = hint.match
    report = LabReport(
        patient_id=match.patient.id if match.patient else None,
        patient_match=match.method,
        collected_on=_date(data.get("collected_on")),
        reported_on=_date(data.get("reported_on")),
        lab_name=_text(data.get("lab_name"), 200),
        attachment_id=attachment.id if attachment is not None else None,
        source=source,
        source_message_id=source_message_id,
        raw_extraction=data,
    )
    matcher = AnalyteMatcher(db)
    items = data.get("results") if isinstance(data.get("results"), list) else []
    items = [i for i in items if isinstance(i, dict)]
    # The decimal separator the lab uses, from its unambiguous numbers.
    convention = decimal_convention(
        i.get(k) for i in items for k in ("value", "ref_low", "ref_high", "ref_text")
    )
    ambiguous = False
    for item in items:
        name = _text(item.get("name"), 300)
        if name is None:
            continue
        value_text = _text(item.get("value"), 300)
        unit = _text(item.get("unit"), 50)
        ref_low, ref_high = parse_range(
            _text(item.get("ref_low"), 50),
            _text(item.get("ref_high"), 50),
            item.get("ref_text"),
            convention,
        )
        reading = read_value(value_text)
        value_num = resolve_value(
            reading, convention=convention, ref_low=ref_low, ref_high=ref_high
        )
        comparator = reading.comparator if value_num is not None else None
        ambiguous = ambiguous or (reading.ambiguous and value_num is None)
        lab_flag = item.get("flag") if item.get("flag") in FLAGS else None
        analyte = matcher.resolve(name, item.get("specimen"), unit)
        report.results.append(
            LabResult(
                analyte_id=analyte.id,
                position=len(report.results),
                panel=_text(item.get("panel"), 200),
                raw_name=name,
                value_text=value_text,
                value_num=value_num,
                comparator=comparator,
                unit=unit,
                ref_low=ref_low,
                ref_high=ref_high,
                ref_text=_text(item.get("ref_text"), 300),
                flag=compute_flag(lab_flag, value_num, comparator, ref_low, ref_high),
            )
        )
    # A value whose reading stays ambiguous ("250.000" with no range or
    # convention to tell 250 from 250000) is kept as text only: review it.
    # A patient chosen only by the uploader's hint (``manual``) is confirmed
    # by someone: the report stays «Por revisar».
    report.status = (
        "ok"
        if confident(report.patient_id, report.patient_match, report.results, ambiguous)
        else "needs_review"
    )
    db.add(report)
    db.flush()
    after = audit.snapshot(report)
    after["results"] = [audit.snapshot(r) for r in report.results]
    audit.record(
        db,
        user_id=None,
        action=audit.ACTION_CREATE,
        entity="lab_report",
        entity_id=report.id,
        after=after,
    )
    unlinked = [
        a.id for a in [attachment, *extra_attachments] if a is not None and a.owner_type is None
    ]
    if unlinked:
        link_attachments(db, unlinked, owner_type="lab_report", owner_id=report.id, user_id=None)
    return report


def _log_usage(attachment_id: int, result: extract.Extraction, usage: dict[str, Any]) -> None:
    logger.info(
        "lab extraction: attachment %s %s; requests=%s input=%s output=%s cache_write=%s "
        "cache_read=%s cost_usd=%s",
        attachment_id,
        result.error or ("lab_report" if result.data["is_lab_report"] else "not_lab_report"),
        usage["requests"],
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cache_creation_input_tokens"],
        usage["cache_read_input_tokens"],
        usage["cost_usd"],
    )


def load_attachments(db: DbSession, attachment_ids: Sequence[int]) -> list[Attachment] | None:
    """The attachments in the given order, or None when one is missing."""
    rows = {
        a.id: a for a in db.scalars(select(Attachment).where(Attachment.id.in_(attachment_ids)))
    }
    if not attachment_ids or any(i not in rows for i in attachment_ids):
        return None
    return [rows[i] for i in attachment_ids]


def check_files(attachments: Sequence[Attachment]) -> str | None:
    """None when the files can go to Claude as one document, else a Spanish
    message: one PDF alone, or photos (JPEG, PNG, WebP) only, distinct."""
    mimes = [a.mime for a in attachments]
    if not mimes:
        return "Elige un PDF o al menos una foto."
    if PDF.mime in mimes:
        return None if len(mimes) == 1 else "Sube un solo PDF, sin otras fotos ni archivos."
    if any(m not in extract.IMAGE_MEDIA_TYPES for m in mimes):
        if any(m in ("image/heic", "image/heif") for m in mimes):
            return (
                "Las fotos HEIC no se pueden leer. Configura la cámara en «Más compatible» "
                "(JPG) o sube una captura de pantalla de la foto."
            )
        return "Solo se aceptan un PDF o fotos (JPG, PNG o WebP)."
    if len(mimes) > extract.MAX_IMAGES:
        return f"Sube como máximo {extract.MAX_IMAGES} fotos por informe."
    if len({a.sha256 for a in attachments}) != len(attachments):
        return "Hay fotos repetidas."
    return None


def too_large(settings: Settings, attachments: Sequence[Attachment]) -> bool:
    cap = max(1, settings.lab_pdf_max_mb) * MB
    if attachments[0].mime == PDF.mime:
        return attachments[0].size > cap
    return (
        any(a.size > extract.IMAGE_MAX_BYTES for a in attachments)
        or sum(a.size for a in attachments) > cap
    )


def _run(
    db: DbSession,
    storage: Storage,
    settings: Settings,
    attachments: list[Attachment],
    *,
    source_message_id: int | None,
    patient_hint_id: int | None,
    client,
    progress: Progress | None,
    source: str | None = None,
) -> Outcome:
    report_progress = progress or (lambda percent, message: None)
    first = attachments[0]
    is_pdf = first.mime == PDF.mime
    key = extraction_key(attachments)

    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
    done = db.scalar(select(LabExtraction).where(LabExtraction.sha256 == key))
    if done is not None:
        logger.info("lab extraction: attachment %s already processed", first.id)
        return Outcome("duplicate", done.report_id, done.error)

    if too_large(settings, attachments):
        logger.info("lab extraction: attachment %s over the size cap", first.id)
        return _record(db, key, first, "failed", error="too_large")
    if not settings.anthropic_api_key:
        raise jobs.PermanentJobError("La lectura automática de laboratorios no está configurada.")

    model = settings.extraction_model
    if client is None:
        client = extract.make_client(settings.anthropic_api_key)
    if is_pdf:
        report_progress(10, "Descargando el PDF")
        pdf = _read_object(storage, first.storage_key, max(1, settings.lab_pdf_max_mb) * MB)
        if pdf is None:
            return _record(db, key, first, "failed", error="too_large")
        report_progress(30, "Leyendo el PDF")
        result = extract.extract_pdf(client, model, pdf)
        del pdf
    else:
        report_progress(10, "Descargando las fotos")
        images: list[tuple[str, bytearray]] = []
        for attachment in attachments:
            data = _read_object(storage, attachment.storage_key, extract.IMAGE_MAX_BYTES)
            if data is None:
                return _record(db, key, first, "failed", error="too_large")
            images.append((attachment.mime, data))
        report_progress(30, "Leyendo las fotos")
        result = extract.extract_images(client, model, images)
        del images
    usage = _usage_summary(result, model)
    _log_usage(first.id, result, usage)

    if result.error:
        if result.config_error:
            raise jobs.PermanentJobError("No se pudo usar el servicio de lectura de documentos.")
        if result.retryable:
            raise jobs.JobError("El servicio de lectura de documentos no respondió.")
        return _record(db, key, first, "failed", error=result.error, model=model, usage=usage)
    if not result.data["is_lab_report"]:
        return _record(db, key, first, "not_lab_report", model=model, usage=usage)

    report_progress(80, "Guardando resultados")
    if source_message_id is not None and db.get(WhatsappMessage, source_message_id) is None:
        source_message_id = None
    report = save_report(
        db,
        result.data,
        attachment=first,
        extra_attachments=attachments[1:],
        source=source or ("whatsapp" if source_message_id is not None else "upload"),
        source_message_id=source_message_id,
        patient_hint_id=patient_hint_id,
        ask=patient_llm.Asker(settings),
    )
    return _record(db, key, first, "lab_report", report=report, model=model, usage=usage)


def process_attachment(
    db: DbSession,
    storage: Storage,
    settings: Settings,
    attachment_id: int,
    *,
    source_message_id: int | None = None,
    source: str | None = None,
    client=None,
    progress: Progress | None = None,
) -> Outcome:
    """Extract lab results from one stored PDF — or one WhatsApp photo the
    classifier called a lab report (#37; key = its SHA-256, like a one-page
    upload) — at most once per SHA-256.
    ``source`` overrides the report's source (``link`` for a downloaded link)."""
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise jobs.PermanentJobError("El archivo ya no existe.")
    if attachment.mime != PDF.mime and attachment.mime not in extract.IMAGE_MEDIA_TYPES:
        raise jobs.PermanentJobError("El archivo no es un PDF ni una foto que se pueda leer.")
    return _run(
        db,
        storage,
        settings,
        [attachment],
        source_message_id=source_message_id,
        patient_hint_id=None,
        client=client,
        progress=progress,
        source=source,
    )


def process_upload(
    db: DbSession,
    storage: Storage,
    settings: Settings,
    attachment_ids: Sequence[int],
    *,
    patient_hint_id: int | None = None,
    source: str | None = None,
    client=None,
    progress: Progress | None = None,
) -> Outcome:
    """Extract lab results from a manual upload — one PDF, or photos of the
    pages of one report in page order — at most once per extraction key.
    ``source`` overrides the report's source (``link`` for files downloaded
    by hand from a lab link)."""
    attachments = load_attachments(db, attachment_ids)
    if attachments is None:
        raise jobs.PermanentJobError("El archivo ya no existe.")
    if check_files(attachments) is not None:
        raise jobs.PermanentJobError("Los archivos no son un PDF ni fotos que se puedan leer.")
    return _run(
        db,
        storage,
        settings,
        attachments,
        source_message_id=None,
        patient_hint_id=patient_hint_id,
        client=client,
        progress=progress,
        source=source,
    )
