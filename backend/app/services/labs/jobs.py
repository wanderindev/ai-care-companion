"""Worker side of lab extraction (listed in ``app.worker.HANDLER_MODULES``).

- ``after_import`` hook: when a ``labs_only`` WhatsApp import finishes, queue
  one ``lab_extract`` job per new PDF it stored (``pdf_messages``), skipping
  files whose SHA-256 was already processed or is already queued by this
  import. It only enqueues (in the import's transaction): no Claude call
  there. ``daily``/``backfill`` imports skip it: their PDFs and images go
  through the media classifier (``app.services.media_classification``, #37),
  which queues ``lab_extract`` only for what it calls a lab report — one
  cheap call per file instead of the extraction model reading every PDF.
- ``lab_extract`` handler. Payload ``{"attachment_id": id,
  "source_message_id": id | null}`` (WhatsApp; ``process.process_attachment``)
  — a PDF, or a single WhatsApp photo the media classifier called a lab
  report — or ``{"attachment_ids": [ids in page order], "patient_id": id | null}``
  (manual upload, #15; ``process.process_upload``). A file problem recorded
  as ``failed`` is committed and then fails the job (no retry) with a
  generic Spanish message. An optional ``"lab_link_id"`` (a PDF downloaded
  from a lab link, by the worker or by hand) makes the WhatsApp-style
  payload's report — or the manual upload's — ``source = 'link'`` and gives
  the link its ``report_id`` (the first one: a link may hold several PDFs,
  each its own job and report, #124).
- ``after_import`` hook ``record_lab_links``: lab portal links in the
  import's new messages → ``lab_links`` rows (``links.record_links``), and a
  ``lab_link_fetch`` job for each allow-listed one.
- ``lab_link_fetch`` handler. Payload ``{"lab_link_id": id}``: download with
  ``fetch.fetch_pdf``; a PDF is stored as an attachment (SHA-256 dedupe),
  the link marked ``fetched`` and a ``lab_extract`` job queued (unless that
  content was already read). Anything else keeps the link ``pending`` with a
  reason code; network trouble retries with the job's backoff and, on the
  last attempt, leaves it ``pending`` (``unreachable``). Logs carry the link
  id and reason only, never the URL.
- ``analyte_triage`` handler (#100, ``triage.run_triage``). Payload
  ``{"user_id": id | null}`` (the admin who started it from «Revisar
  exámenes»). Proposals only: the page never auto-applies (that is the CLI's
  ``--apply``). Each batch is committed, so a retry continues where it stopped.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.config import get_settings
from app.models.attachment import Attachment
from app.models.lab import LabExtraction, LabLink
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import audit, jobs
from app.services.attachments import PDF, store_upload
from app.services.labs import fetch, links, triage
from app.services.labs.process import (
    JOB_KIND,
    enqueue_extraction,
    process_attachment,
    process_upload,
)

# Importing it also registers its after_import hook, so a process that loads
# this module never skips daily/backfill PDFs without classifying them.
from app.services.media_classification.jobs import CLASSIFY_MODES
from app.services.storage import StorageNotConfigured, get_storage
from app.services.whatsapp_import import after_import, after_media, pdf_messages

DONE_MESSAGES = {
    "lab_report": "Informe de laboratorio guardado",
    "not_lab_report": "El PDF no es un informe de laboratorio",
    "duplicate": "Este PDF ya se había procesado",
}
FAILED_MESSAGES = {
    "too_large": "El PDF es demasiado grande para leerlo automáticamente.",
}
FAILED_DEFAULT = "No se pudo leer el PDF automáticamente."
PHOTO_DONE_MESSAGES = {
    "lab_report": "Informe de laboratorio guardado",
    "not_lab_report": "Las fotos no son de un informe de laboratorio",
    "duplicate": "Estas fotos ya se habían procesado",
}
PHOTO_FAILED_MESSAGES = {
    "too_large": "Las fotos son demasiado grandes para leerlas automáticamente.",
}
PHOTO_FAILED_DEFAULT = "No se pudieron leer las fotos automáticamente."
UNREACHABLE = "unreachable"
LINK_DONE_MESSAGES = {
    "fetched": "PDF descargado; se leerá en breve",
    "pending": "No se pudo descargar el PDF; queda pendiente",
}

logger = logging.getLogger("app.labs")


@after_media
@after_import
def enqueue_lab_extractions(db: DbSession, imp: WhatsappImport) -> None:
    if imp.mode in CLASSIFY_MODES:
        return  # the media classifier decides (and queues lab_extract itself)
    messages = pdf_messages(db, imp.id)
    if not messages:
        return
    attachments = {
        a.id: a
        for a in db.scalars(
            select(Attachment).where(Attachment.id.in_([m.attachment_id for m in messages]))
        )
    }
    shas = {a.sha256 for a in attachments.values()}
    seen = set(db.scalars(select(LabExtraction.sha256).where(LabExtraction.sha256.in_(shas))))
    for message in messages:
        attachment = attachments.get(message.attachment_id)
        if attachment is None or attachment.sha256 in seen:
            continue
        seen.add(attachment.sha256)
        enqueue_extraction(db, attachment.id, source_message_id=message.id)


def _optional_id(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _id_list(value) -> list[int] | None:
    if not isinstance(value, list) or not value:
        return None
    ids = [_optional_id(v) for v in value]
    return None if any(i is None for i in ids) else ids


@after_import
def record_lab_links(db: DbSession, imp: WhatsappImport) -> None:
    messages = db.scalars(
        select(WhatsappMessage)
        .where(WhatsappMessage.import_id == imp.id, WhatsappMessage.text.ilike("%http%"))
        .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
    )
    created = links.record_links(db, messages, get_settings())
    if created:
        logger.info("lab links: import %s recorded %s link(s)", imp.id, len(created))


@jobs.handler(JOB_KIND)
def run_lab_extract(ctx: jobs.JobContext) -> None:
    upload_ids = _id_list(ctx.payload.get("attachment_ids"))
    attachment_id = _optional_id(ctx.payload.get("attachment_id"))
    link_id = _optional_id(ctx.payload.get("lab_link_id"))
    if upload_ids is None and attachment_id is None:
        raise jobs.PermanentJobError("Tarea de laboratorio inválida.")
    try:
        storage = get_storage()
    except StorageNotConfigured:
        raise jobs.JobError("El almacenamiento de archivos no está disponible.") from None
    photos = False
    if upload_ids is not None:
        outcome = process_upload(
            ctx.db,
            storage,
            get_settings(),
            upload_ids,
            patient_hint_id=_optional_id(ctx.payload.get("patient_id")),
            source="link" if link_id is not None else None,
            progress=ctx.progress,
        )
        first = ctx.db.get(Attachment, upload_ids[0])
        photos = first is not None and first.mime != PDF.mime
    else:
        outcome = process_attachment(
            ctx.db,
            storage,
            get_settings(),
            attachment_id,
            source_message_id=_optional_id(ctx.payload.get("source_message_id")),
            source="link" if link_id is not None else None,
            progress=ctx.progress,
        )
        single = ctx.db.get(Attachment, attachment_id)
        photos = single is not None and single.mime != PDF.mime
    if link_id is not None and outcome.report_id is not None:
        link = ctx.db.get(LabLink, link_id)
        # A link may hold several PDFs (#124): it keeps its first report.
        if link is not None and link.report_id is None:
            _update_link(ctx.db, link, user_id=None, report_id=outcome.report_id)
    ctx.db.commit()
    if outcome.status == "failed":
        messages = PHOTO_FAILED_MESSAGES if photos else FAILED_MESSAGES
        default = PHOTO_FAILED_DEFAULT if photos else FAILED_DEFAULT
        raise jobs.PermanentJobError(messages.get(outcome.error or "", default))
    ctx.progress(100, (PHOTO_DONE_MESSAGES if photos else DONE_MESSAGES)[outcome.status])


def _update_link(db: DbSession, link: LabLink, *, user_id: int | None, **values) -> None:
    before = audit.snapshot(link)
    for key, value in values.items():
        setattr(link, key, value)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity="lab_link",
        entity_id=link.id,
        before=before,
        after=audit.snapshot(link),
    )


def _keep_pending(ctx: jobs.JobContext, link: LabLink, reason: str) -> None:
    if link.reason != reason:
        _update_link(ctx.db, link, user_id=None, reason=reason)
    logger.info("lab link %s: not fetched (%s)", link.id, reason)
    ctx.db.commit()
    ctx.progress(100, LINK_DONE_MESSAGES["pending"])


@jobs.handler(links.FETCH_JOB_KIND)
def run_lab_link_fetch(ctx: jobs.JobContext) -> None:
    link_id = _optional_id(ctx.payload.get("lab_link_id"))
    if link_id is None:
        raise jobs.PermanentJobError("Tarea de enlace inválida.")
    link = ctx.db.get(LabLink, link_id)
    if link is None:
        raise jobs.PermanentJobError("El enlace ya no existe.")
    if link.status != "pending":
        ctx.progress(100, "El enlace ya no está pendiente")
        return
    settings = get_settings()
    patterns = settings.lab_link_host_patterns
    if not links.fetchable(link, settings):
        _keep_pending(ctx, link, links.NOT_ALLOWED)
        return
    try:
        storage = get_storage()
    except StorageNotConfigured:
        raise jobs.JobError("El almacenamiento de archivos no está disponible.") from None
    url = link.url
    # Release the transaction (and any row snapshot) while downloading.
    ctx.db.commit()
    ctx.progress(10, "Descargando el enlace")
    try:
        result = fetch.fetch_pdf(url, patterns, max(1, settings.lab_pdf_max_mb) * fetch.MB)
    except fetch.TransientFetchError:
        link = ctx.db.get(LabLink, link_id)
        if link is None or link.status != "pending":
            return
        if ctx.attempt >= links.FETCH_MAX_ATTEMPTS:
            _keep_pending(ctx, link, UNREACHABLE)
            return
        if link.reason != UNREACHABLE:
            _update_link(ctx.db, link, user_id=None, reason=UNREACHABLE)
            ctx.db.commit()
        logger.info("lab link %s: fetch attempt %s failed (network)", link_id, ctx.attempt)
        raise jobs.JobError("El laboratorio no respondió; se reintentará.") from None

    link = ctx.db.get(LabLink, link_id, with_for_update=True)
    if link is None or link.status != "pending":  # ignored or uploaded meanwhile
        ctx.db.commit()
        ctx.progress(100, "El enlace ya no está pendiente")
        return
    if result.data is None:
        _keep_pending(ctx, link, result.reason or fetch.NOT_PDF)
        return

    attachment = store_upload(
        ctx.db,
        storage,
        data=result.data,
        file_type=PDF,
        filename="resultado.pdf",
        user_id=None,
    )
    del result
    done = ctx.db.scalar(select(LabExtraction).where(LabExtraction.sha256 == attachment.sha256))
    _update_link(
        ctx.db,
        link,
        user_id=None,
        status="fetched",
        reason=None,
        attachment_id=attachment.id,
        report_id=done.report_id if done is not None else None,
    )
    if done is None:
        enqueue_extraction(
            ctx.db, attachment.id, source_message_id=link.message_id, lab_link_id=link.id
        )
    logger.info("lab link %s: PDF stored as attachment %s", link.id, attachment.id)
    ctx.db.commit()
    ctx.progress(100, LINK_DONE_MESSAGES["fetched"])


@jobs.handler(triage.JOB_KIND)
def run_analyte_triage(ctx: jobs.JobContext) -> None:
    user_id = _optional_id(ctx.payload.get("user_id"))
    stats = triage.run_triage(ctx.db, get_settings(), user_id=user_id, progress=ctx.progress)
    usage = stats.usage.as_dict()
    logger.info(
        "analyte triage job %s: batches=%d proposals=%d errors=%d input_tokens=%d output_tokens=%d",
        ctx.job_id,
        stats.batches,
        sum(stats.proposals.values()),
        sum(stats.errors.values()),
        usage["input_tokens"],
        usage["output_tokens"],
    )
    done = sum(stats.proposals.values())
    ctx.progress(100, f"Revisión con IA terminada: {done} propuestas")
