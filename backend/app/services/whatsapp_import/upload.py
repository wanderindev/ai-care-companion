"""Chunked upload of a WhatsApp export zip (several GB) through the API.

The browser cannot PUT straight to Spaces (that would need a CORS rule on the
bucket), and one request cannot carry the zip (the edge allows 200 MB). So:

1. ``start_upload`` creates the ``whatsapp_imports`` row and an S3 multipart
   upload at ``archives/<yyyy>/upload-<id>-<random>.zip``, and fixes the chunk
   size and count.
2. Each ``PUT …/chunks/{index}`` stores chunk ``index`` as part ``index + 1``
   at once (``put_chunk``). Chunks may arrive in any order and be re-sent
   (a re-sent part replaces the earlier one), and nothing is kept in process
   memory between requests, so any API worker can take any chunk.
3. ``complete_upload`` checks with S3 that every part is there with the right
   size, completes the multipart upload and enqueues the import job.

S3 parts must be ≥ 5 MB except the last, hence ``MIN_CHUNK_BYTES``.
Abandoned uploads are aborted by ``cancel_upload`` (the UI calls it when the
user gives up); an upload left behind otherwise keeps its parts in the
bucket until someone cancels it.
"""

import math
import secrets
from datetime import UTC, datetime

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings
from app.models.whatsapp import WhatsappImport
from app.services import audit, jobs
from app.services.attachments import ZIP
from app.services.storage import Storage

JOB_KIND = "whatsapp_import"
MIN_CHUNK_BYTES = 5 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_PARTS = 10_000


class UploadError(ValueError):
    """A client mistake (message is Spanish and safe to show); ``status`` is HTTP."""

    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status = status
        # A stable machine-readable reason (sent as ``X-Error-Code``), when useful.
        self.code = code


def chunk_bytes(settings: Settings) -> int:
    size = settings.whatsapp_chunk_mb * 1024 * 1024
    return max(MIN_CHUNK_BYTES, min(MAX_CHUNK_BYTES, size))


def max_import_bytes(settings: Settings) -> int:
    return settings.whatsapp_import_max_mb * 1024 * 1024


def expected_chunk_size(imp: WhatsappImport, index: int) -> int:
    if index < imp.chunk_count - 1:
        return imp.chunk_size
    return imp.size - imp.chunk_size * (imp.chunk_count - 1)


def start_upload(
    db: DbSession,
    storage: Storage,
    settings: Settings,
    *,
    user_id: int,
    mode: str,
    filename: str,
    size: int,
    patient_focus_id: int | None,
    awaiting_media: bool = False,
    date_order: str | None = None,
) -> WhatsappImport:
    """Create the import row and its multipart upload. The caller commits.
    ``awaiting_media``: a sync extension run that uploads files afterwards
    (``media_upload``). ``date_order`` (``dmy``/``mdy``/``ymd``): the
    uploader's day/month order, kept in ``stats.date_order_hint`` for
    ``process_import`` (used only when the file's dates read both ways)."""
    if size <= 0:
        raise UploadError("El archivo está vacío.")
    if size > max_import_bytes(settings):
        raise UploadError(
            f"El archivo supera el límite de {settings.whatsapp_import_max_mb} MB.", 413
        )
    chunk = chunk_bytes(settings)
    count = math.ceil(size / chunk)
    if count > MAX_PARTS:  # only with a tiny chunk size and a huge limit
        raise UploadError("El archivo es demasiado grande.", 413)

    imp = WhatsappImport(
        uploaded_by=user_id,
        mode=mode,
        patient_focus_id=patient_focus_id,
        status="uploading",
        filename=(filename or "chat.zip").replace("\\", "/").rsplit("/", 1)[-1][:255] or "chat.zip",
        size=size,
        chunk_size=chunk,
        chunk_count=count,
        stats={"date_order_hint": date_order} if date_order else {},
        awaiting_media=awaiting_media,
    )
    db.add(imp)
    db.flush()
    year = datetime.now(UTC).year
    imp.staging_key = f"{ZIP.kind}/{year:04d}/upload-{imp.id}-{secrets.token_hex(8)}.zip"
    imp.upload_id = storage.create_multipart(imp.staging_key, ZIP.mime)
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity="whatsapp_import",
        entity_id=imp.id,
        after=audit.snapshot(imp),
    )
    return imp


def _require_uploading(imp: WhatsappImport) -> None:
    if imp.status != "uploading" or not imp.upload_id or not imp.staging_key:
        raise UploadError("Esta importación ya no está recibiendo archivos.", 409)


def put_chunk(storage: Storage, imp: WhatsappImport, index: int, data: bytes) -> None:
    """Store chunk ``index`` (0-based) as a part. Re-sending a chunk replaces it."""
    _require_uploading(imp)
    if not 0 <= index < imp.chunk_count:
        raise UploadError("Parte del archivo fuera de rango.")
    if len(data) != expected_chunk_size(imp, index):
        raise UploadError("La parte del archivo no tiene el tamaño esperado.")
    storage.upload_part(imp.staging_key, imp.upload_id, index + 1, data)


def received_chunks(storage: Storage, imp: WhatsappImport) -> list[int]:
    """0-based indexes of the chunks already stored with the right size."""
    if imp.status != "uploading" or not imp.upload_id:
        return []
    parts = storage.list_parts(imp.staging_key, imp.upload_id)
    return sorted(
        p["PartNumber"] - 1
        for p in parts
        if 1 <= p["PartNumber"] <= imp.chunk_count
        and p["Size"] == expected_chunk_size(imp, p["PartNumber"] - 1)
    )


def _no_such_upload(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in {"NoSuchUpload", "404"}


def complete_upload(
    db: DbSession, storage: Storage, imp: WhatsappImport, *, user_id: int
) -> WhatsappImport:
    """Assemble the parts and enqueue the import job. The caller holds the row
    lock (``SELECT … FOR UPDATE``) and commits. Idempotent: completing an import
    that is already queued (or further) just returns it."""
    if imp.status != "uploading":
        if imp.status == "cancelled":
            raise UploadError("Esta importación fue cancelada.", 409)
        return imp
    _require_uploading(imp)
    before = audit.snapshot(imp)
    try:
        parts = storage.list_parts(imp.staging_key, imp.upload_id)
    except ClientError as exc:
        # A completion whose database commit failed afterwards: the object is
        # already assembled, so just finish the bookkeeping.
        if not (_no_such_upload(exc) and storage.exists(imp.staging_key)):
            raise
        parts = None
    if parts is not None:
        by_number = {p["PartNumber"]: p for p in parts}
        missing = [
            i
            for i in range(imp.chunk_count)
            if (part := by_number.get(i + 1)) is None or part["Size"] != expected_chunk_size(imp, i)
        ]
        if missing:
            raise UploadError("Faltan partes del archivo. Vuelve a intentarlo.", 409)
        storage.complete_multipart(
            imp.staging_key,
            imp.upload_id,
            [by_number[i + 1] for i in range(imp.chunk_count)],
        )
    imp.upload_id = None
    imp.status = "queued"
    job = jobs.enqueue(db, JOB_KIND, {"import_id": imp.id}, message="En cola")
    imp.job_id = job.id
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity="whatsapp_import",
        entity_id=imp.id,
        before=before,
        after=audit.snapshot(imp),
    )
    return imp


def cancel_upload(
    db: DbSession, storage: Storage, imp: WhatsappImport, *, user_id: int
) -> WhatsappImport:
    """Abort an unfinished upload (its parts are deleted). The caller commits."""
    if imp.status == "cancelled":
        return imp
    _require_uploading(imp)
    before = audit.snapshot(imp)
    try:
        storage.abort_multipart(imp.staging_key, imp.upload_id)
    except ClientError as exc:
        if not _no_such_upload(exc):
            raise
    imp.upload_id = None
    imp.staging_key = None
    imp.status = "cancelled"
    imp.finished_at = datetime.now(UTC)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity="whatsapp_import",
        entity_id=imp.id,
        before=before,
        after=audit.snapshot(imp),
    )
    return imp
