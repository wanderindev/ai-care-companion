"""Worker side of media classification (listed in ``app.worker.HANDLER_MODULES``).

- ``after_import`` hook ``enqueue_classifications``: when a ``daily`` or
  ``backfill`` import finishes (never ``labs_only``: its PDFs go straight to
  ``lab_extract``, unchanged), every image and PDF the import stored is
  matched by SHA-256 against ``media_classifications``: a known result is
  copied to the message at once; otherwise one ``classify_media`` job is
  queued per distinct hash. It only enqueues (in the import's transaction):
  no Claude call there. ``labs.jobs.enqueue_lab_extractions`` skips these
  modes, so a PDF is read by the expensive extraction model only when the
  classifier calls it a lab report.
- ``classify_media`` handler. Payload ``{"attachment_id": id,
  "source_message_id": id | null}``. Under a per-hash advisory lock it
  re-checks ``media_classifications`` (a second job for the same file only
  copies the result), then decides **once per file content**:

  1. a WhatsApp sticker (a WebP ≤ ``STICKER_MAX_BYTES`` whose header, read
     without decoding, says square and ≤ ``STICKER_MAX_SIDE`` px: WhatsApp
     stickers are 512×512) → ``other``, no call;
  2. content already read as a lab report by the labs pipeline
     (``lab_extractions.outcome = 'lab_report'``) → ``lab_report``, no call;
  3. otherwise one Claude call (``classify.classify``).

  The result is stored in ``media_classifications`` and in
  ``whatsapp_messages.classification`` of every message whose file has that
  hash. A ``lab_report`` from Claude queues one ``lab_extract`` job
  (``enqueue_extraction``, PDF or single photo, key = its SHA-256) unless
  ``lab_extractions`` already has that content — so each file is sent at
  most once to the classifier and at most once to the extractor.

``whatsapp_messages.classification``::

    {"status": "done", "category": …, "description": …, "patient_hint": …,
     "method": "claude" | "sticker" | "lab_extraction", "sha256": …}
    {"status": "failed", "error": "<code>", "sha256": …}

Failures: no key / a key or model the API refuses (401/403/404) → the job
fails at once with a generic message and nothing is recorded (re-run it once
the settings are fixed). A file over the caps (image > 5 MB, PDF >
``LAB_PDF_MAX_MB``), of a type Claude does not read (HEIC), or refused /
unreadable (400/413/422, refusal, truncated or invalid output) is recorded as
``failed`` with a short code, so it is never sent again, and fails the job
without retrying. Every recorded failure except ``too_large`` and
``unsupported`` (which the extractor would refuse the same way) still queues
``lab_extract`` for the file — PDF or photo — so a lab report is never lost
because the cheap classifier could not read it (the extractor makes its own
lab / not-lab decision, once per SHA-256). Timeouts, network errors, 429 and 5xx retry with the job's
backoff. Logs carry ids, the category, sizes and token counts only — never
the description, the patient hint or file names.

Known limit: photos of the pages of one report sent as separate WhatsApp
images are classified and extracted one page at a time.
"""

import logging

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.config import get_settings
from app.models.attachment import Attachment
from app.models.lab import LabExtraction
from app.models.media_classification import MediaClassification
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.attachments import PDF, WEBP
from app.services.labs import extract as lab_extract
from app.services.labs.process import enqueue_extraction
from app.services.media_classification import classify
from app.services.storage import Storage, StorageNotConfigured, get_storage
from app.services.whatsapp_import import after_import, after_media

JOB_KIND = "classify_media"
CLASSIFY_MODES = frozenset({"daily", "backfill"})
IMAGE_KIND = "images"  # attachments.kind (storage prefix) of photos
# WhatsApp stickers are 512×512 WebP (≤ 100 KB static, ≤ 500 KB animated);
# WhatsApp sends real photos as JPEG. Any other WebP (larger, not square, or a
# header we cannot read) is classified like any photo.
STICKER_MAX_BYTES = 512 * 1024
STICKER_MAX_SIDE = 512
WEBP_HEADER_BYTES = 30
# Failures the extractor would hit the same way: no lab_extract fallback.
NO_LAB_FALLBACK = frozenset({"too_large", "unsupported"})
STICKER_DESCRIPTION = "Sticker"
LAB_DESCRIPTION = "Informe de laboratorio"
MB = 1024 * 1024

NOT_CONFIGURED = "La clasificación automática de archivos no está configurada."
CONFIG_REFUSED = "No se pudo usar el servicio de lectura de documentos."
RETRY_MESSAGE = "No se pudo clasificar el archivo por ahora; se volverá a intentar."
FAILED_MESSAGES = {
    "too_large": "El archivo es demasiado grande para clasificarlo automáticamente.",
    "unsupported": "Este tipo de archivo no se puede clasificar automáticamente.",
}
FAILED_DEFAULT = "No se pudo clasificar el archivo automáticamente."
DONE_MESSAGES = {
    "lab_report": "Informe de laboratorio; se leerán sus resultados",
    "prescription": "Clasificado como receta",
    "medical_document": "Clasificado como documento médico",
    "other": "Clasificado: sin contenido médico",
}

logger = logging.getLogger("app.media_classification")


def enqueue_classification(db: DbSession, attachment_id: int, *, source_message_id: int | None):
    """Queue a ``classify_media`` job for one attachment (caller commits)."""
    return jobs.enqueue(
        db,
        JOB_KIND,
        {"attachment_id": attachment_id, "source_message_id": source_message_id},
        message="En cola",
    )


def message_value(row: MediaClassification) -> dict:
    """What ``whatsapp_messages.classification`` holds for a stored result."""
    if row.status == "done":
        return {
            "status": "done",
            "category": row.category,
            "description": row.description,
            "patient_hint": row.patient_hint,
            "method": row.method,
            "sha256": row.sha256,
        }
    return {"status": "failed", "error": row.error, "sha256": row.sha256}


def apply_classification(db: DbSession, row: MediaClassification) -> int:
    """Copy ``row`` to every message whose image/PDF has its hash and no
    classification yet; returns how many messages changed."""
    files = select(Attachment.id).where(
        Attachment.sha256 == row.sha256,
        or_(Attachment.kind == IMAGE_KIND, Attachment.mime == PDF.mime),
    )
    result = db.execute(
        update(WhatsappMessage)
        .where(WhatsappMessage.attachment_id.in_(files), WhatsappMessage.classification.is_(None))
        .values(classification=message_value(row))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


@after_media
@after_import
def enqueue_classifications(db: DbSession, imp: WhatsappImport) -> None:
    if imp.mode not in CLASSIFY_MODES:
        return
    rows = db.execute(
        select(WhatsappMessage.id, Attachment.id, Attachment.sha256)
        .join(Attachment, Attachment.id == WhatsappMessage.attachment_id)
        .where(
            WhatsappMessage.media_import_id == imp.id,
            or_(Attachment.kind == IMAGE_KIND, Attachment.mime == PDF.mime),
        )
        .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
    ).all()
    if not rows:
        return
    known = {
        c.sha256: c
        for c in db.scalars(
            select(MediaClassification).where(MediaClassification.sha256.in_({r[2] for r in rows}))
        )
    }
    handled: set[str] = set()
    reused = queued = 0
    for message_id, attachment_id, sha in rows:
        if sha in handled:
            continue
        handled.add(sha)
        row = known.get(sha)
        if row is not None:
            reused += apply_classification(db, row)
            continue
        enqueue_classification(db, attachment_id, source_message_id=message_id)
        queued += 1
    logger.info(
        "media classification: import %s has %s image/PDF message(s), %s job(s) queued, "
        "%s message(s) reused a result",
        imp.id,
        len(rows),
        queued,
        reused,
    )


def _store(db: DbSession, attachment: Attachment, **values) -> MediaClassification:
    row = MediaClassification(sha256=attachment.sha256, attachment_id=attachment.id, **values)
    db.add(row)
    db.flush()
    apply_classification(db, row)
    return row


def _record_failure(
    db: DbSession,
    attachment: Attachment,
    code: str,
    *,
    source_message_id: int | None = None,
    **values,
) -> None:
    _store(db, attachment, status="failed", error=code, **values)
    fallback = code not in NO_LAB_FALLBACK and _queue_lab_extraction(
        db, attachment, source_message_id
    )
    db.commit()
    logger.warning(
        "media classification: attachment %s failed for good: %s%s",
        attachment.id,
        code,
        "; queued for lab extraction" if fallback else "",
    )
    raise jobs.PermanentJobError(FAILED_MESSAGES.get(code, FAILED_DEFAULT))


def _read_object(storage: Storage, key: str, cap: int) -> bytearray | None:
    data = bytearray()
    for chunk in storage.iter_object(key):
        if len(data) + len(chunk) > cap:
            return None
        data += chunk
    return data


def webp_dimensions(head: bytes) -> tuple[int, int] | None:
    """Width and height from a WebP file's first bytes (no decoding), or None
    when the header is not one of the three WebP forms (VP8, VP8L, VP8X)."""
    if len(head) < WEBP_HEADER_BYTES or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    chunk = head[12:16]
    if chunk == b"VP8X":  # extended (animated stickers): 24-bit width-1, height-1
        return (
            int.from_bytes(head[24:27], "little") + 1,
            int.from_bytes(head[27:30], "little") + 1,
        )
    if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":  # lossy key frame
        return (
            int.from_bytes(head[26:28], "little") & 0x3FFF,
            int.from_bytes(head[28:30], "little") & 0x3FFF,
        )
    if chunk == b"VP8L" and head[20] == 0x2F:  # lossless: 14-bit width-1, height-1
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _read_head(storage: Storage, key: str, size: int) -> bytes:
    head = bytearray()
    for chunk in storage.iter_object(key):
        head += chunk
        if len(head) >= size:
            break
    return bytes(head[:size])


def _is_sticker(attachment: Attachment) -> bool:
    if attachment.mime != WEBP.mime or attachment.size > STICKER_MAX_BYTES:
        return False
    try:
        storage = get_storage()
    except StorageNotConfigured:
        raise jobs.JobError("El almacenamiento de archivos no está disponible.") from None
    dims = webp_dimensions(_read_head(storage, attachment.storage_key, WEBP_HEADER_BYTES))
    return dims is not None and dims[0] == dims[1] and dims[0] <= STICKER_MAX_SIDE


def _optional_id(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _queue_lab_extraction(
    db: DbSession, attachment: Attachment, source_message_id: int | None
) -> bool:
    """Send a lab report on to the labs pipeline, once per content."""
    if db.scalar(select(LabExtraction.id).where(LabExtraction.sha256 == attachment.sha256)):
        return False
    if source_message_id is not None and db.get(WhatsappMessage, source_message_id) is None:
        source_message_id = None
    enqueue_extraction(db, attachment.id, source_message_id=source_message_id)
    return True


@jobs.handler(JOB_KIND)
def run_classify_media(ctx: jobs.JobContext) -> None:
    attachment_id = _optional_id(ctx.payload.get("attachment_id"))
    if attachment_id is None:
        raise jobs.PermanentJobError("Tarea de clasificación inválida.")
    source_message_id = _optional_id(ctx.payload.get("source_message_id"))
    db = ctx.db
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise jobs.PermanentJobError("El archivo ya no existe.")
    sha = attachment.sha256

    # One Claude call per file content, even with two jobs for the same hash.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"media_class:{sha}", 0))))
    existing = db.scalar(select(MediaClassification).where(MediaClassification.sha256 == sha))
    if existing is not None:
        filled = apply_classification(db, existing)
        logger.info(
            "media classification: attachment %s already processed (%s); %s message(s) filled",
            attachment_id,
            existing.status,
            filled,
        )
        ctx.progress(100, "Este archivo ya se había clasificado")
        return

    mime = attachment.mime
    if _is_sticker(attachment):
        _store(
            db,
            attachment,
            status="done",
            method="sticker",
            category="other",
            description=STICKER_DESCRIPTION,
            patient_hint="unknown",
        )
        logger.info("media classification: attachment %s is a sticker (no call)", attachment_id)
        ctx.progress(100, DONE_MESSAGES["other"])
        return
    lab = db.scalar(
        select(LabExtraction).where(
            LabExtraction.sha256 == sha, LabExtraction.outcome == "lab_report"
        )
    )
    if lab is not None:
        _store(
            db,
            attachment,
            status="done",
            method="lab_extraction",
            category="lab_report",
            description=LAB_DESCRIPTION,
            patient_hint="unknown",
        )
        logger.info(
            "media classification: attachment %s already read as a lab report (no call)",
            attachment_id,
        )
        ctx.progress(100, "Informe de laboratorio ya leído")
        return

    is_pdf = mime == PDF.mime
    if not is_pdf and mime not in lab_extract.IMAGE_MEDIA_TYPES:
        _record_failure(db, attachment, "unsupported")
    settings = get_settings()
    cap = max(1, settings.lab_pdf_max_mb) * MB if is_pdf else lab_extract.IMAGE_MAX_BYTES
    if attachment.size > cap:
        _record_failure(db, attachment, "too_large")
    if not settings.anthropic_api_key:
        raise jobs.PermanentJobError(NOT_CONFIGURED)
    try:
        storage = get_storage()
    except StorageNotConfigured:
        raise jobs.JobError("El almacenamiento de archivos no está disponible.") from None

    ctx.progress(20, "Clasificando el archivo")
    data = _read_object(storage, attachment.storage_key, cap)
    if data is None:
        _record_failure(db, attachment, "too_large")
    model = settings.classification_model
    result = classify.classify(classify.make_client(settings.anthropic_api_key), model, mime, data)
    del data
    usage = result.usage.as_dict()
    cost = result.usage.cost_usd(model)
    usage["cost_usd"] = round(cost, 6) if cost is not None else None
    logger.info(
        "media classification: attachment %s %s (%s bytes, %s); requests=%s input=%s "
        "output=%s cache_write=%s cache_read=%s cost_usd=%s model=%s",
        attachment_id,
        result.error or result.data["category"],
        attachment.size,
        "pdf" if is_pdf else "image",
        usage["requests"],
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cache_creation_input_tokens"],
        usage["cache_read_input_tokens"],
        usage["cost_usd"],
        model,
    )
    if result.error:
        if result.config_error:
            raise jobs.PermanentJobError(CONFIG_REFUSED)
        if result.retryable:
            logger.warning(
                "media classification: attachment %s attempt %s failed: %s",
                attachment_id,
                ctx.attempt,
                result.error,
            )
            raise jobs.JobError(RETRY_MESSAGE)
        _record_failure(
            db,
            attachment,
            result.error,
            source_message_id=source_message_id,
            method="claude",
            model=model,
            usage=usage,
        )

    category = result.data["category"]
    _store(
        db,
        attachment,
        status="done",
        method="claude",
        category=category,
        description=result.data["description"],
        patient_hint=result.data["patient_hint"],
        model=model,
        usage=usage,
    )
    if category == "lab_report" and _queue_lab_extraction(db, attachment, source_message_id):
        logger.info("media classification: attachment %s queued for lab extraction", attachment_id)
    ctx.progress(100, DONE_MESSAGES[category])
