"""WhatsApp export imports (DESIGN §5.6, §7), admin only.

A zip of several GB is uploaded in chunks (``app.services.whatsapp_import.upload``):

- ``POST /imports/whatsapp`` ``{mode, patient_focus_id?, filename, size}`` →
  the import with its ``chunk_size`` / ``chunk_count``;
- ``PUT /imports/whatsapp/{id}/chunks/{index}`` with the raw bytes of chunk
  ``index`` (``application/octet-stream``), in any order, repeatable;
- ``POST /imports/whatsapp/{id}/complete`` → checks every chunk arrived and
  enqueues the import job (``job_id``; progress via ``GET /jobs/{id}``);
- ``DELETE /imports/whatsapp/{id}`` cancels an unfinished upload;
- ``GET /imports/whatsapp`` (latest first) and ``GET /imports/whatsapp/{id}``
  (with ``received_chunks`` while uploading, to resume).

For the WhatsApp Web sync extension (docs/extension-api.md,
``app.services.whatsapp_import.media_upload``):

- ``GET /imports/whatsapp/last``: the newest stored message's time;
- ``POST /imports/whatsapp`` with ``expects_media: true`` starts a run
  (optional ``date_order``: the day/month order WhatsApp Web shows);
- ``GET /imports/whatsapp/media-missing``: stored messages without their file;
- ``POST /imports/whatsapp/{id}/messages/{message_id}/media`` (multipart
  ``file``): the file of one stored message, through run ``id``;
- ``POST /imports/whatsapp/{id}/media/complete``: no more files for run ``id``.

Every endpoint takes an admin by session cookie or by the extension's bearer
token (``require_import_admin``). Nothing here logs senders, captions or
file names.
"""

import logging
import os
import tempfile
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.api.attachments import STORAGE_UNAVAILABLE, storage_dep
from app.api.deps import require_import_admin
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.patient import Patient
from app.models.user import User
from app.models.whatsapp import MESSAGE_MEDIA_KINDS, WhatsappImport
from app.schemas.whatsapp_imports import (
    ChunkOut,
    LastMessageOut,
    MediaUploadOut,
    MissingMediaItem,
    MissingMediaOut,
    WhatsappImportCreate,
    WhatsappImportOut,
)
from app.services.appointments import to_local
from app.services.storage import Storage
from app.services.whatsapp_import import media_upload, upload

logger = logging.getLogger("app.whatsapp_import")

router = APIRouter(prefix="/imports/whatsapp", tags=["imports"])

LIST_LIMIT = 50
NOT_FOUND = "Importación no encontrada."
# The partial unique index on whatsapp_messages.wa_key (model + migration 0023).
WA_KEY_INDEX = "uq_whatsapp_messages_wa_key"


def _out(imp: WhatsappImport, received: list[int] | None = None) -> WhatsappImportOut:
    effective = imp.status
    if imp.status in {"queued", "processing"} and imp.job and imp.job.status == "failed":
        effective = "failed"
    return WhatsappImportOut(
        id=imp.id,
        mode=imp.mode,
        status=effective,
        patient_focus_id=imp.patient_focus_id,
        filename=imp.filename,
        size=imp.size,
        chunk_size=imp.chunk_size,
        chunk_count=imp.chunk_count,
        job_id=imp.job_id,
        range_from=imp.range_from,
        range_to=imp.range_to,
        stats=imp.stats or {},
        uploaded_by_name=imp.uploader.display_name if imp.uploader else None,
        created_at=imp.created_at,
        finished_at=imp.finished_at,
        awaiting_media=imp.awaiting_media,
        media_completed_at=imp.media_completed_at,
        received_chunks=received,
    )


def _get(db: DbSession, import_id: int, *, lock: bool = False) -> WhatsappImport:
    imp = db.get(WhatsappImport, import_id, with_for_update=lock)
    if imp is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return imp


def _storage_failed(db: DbSession) -> HTTPException:
    db.rollback()
    logger.exception("WhatsApp import storage call failed")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORAGE_UNAVAILABLE
    )


async def chunk_body(request: Request, settings: Settings = Depends(get_settings)) -> bytes:
    """The raw request body, refused as soon as it exceeds one chunk."""
    limit = upload.chunk_bytes(settings)
    too_large = HTTPException(status_code=413, detail="La parte del archivo es demasiado grande.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise too_large
    data = bytearray()
    async for block in request.stream():
        data += block
        if len(data) > limit:
            raise too_large
    return bytes(data)


@router.get("", response_model=list[WhatsappImportOut])
def list_imports(
    db: DbSession = Depends(get_db), _user: User = Depends(require_import_admin)
) -> list[WhatsappImportOut]:
    rows = db.scalars(
        select(WhatsappImport).order_by(WhatsappImport.id.desc()).limit(LIST_LIMIT)
    ).all()
    return [_out(imp) for imp in rows]


@router.post("", response_model=WhatsappImportOut, status_code=status.HTTP_201_CREATED)
def create_import(
    body: WhatsappImportCreate,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_import_admin),
    storage: Storage = Depends(storage_dep),
    settings: Settings = Depends(get_settings),
) -> WhatsappImportOut:
    if body.patient_focus_id is not None and db.get(Patient, body.patient_focus_id) is None:
        raise HTTPException(status_code=400, detail="Paciente no encontrado.")
    try:
        imp = upload.start_upload(
            db,
            storage,
            settings,
            user_id=user.id,
            mode=body.mode,
            filename=body.filename,
            size=body.size,
            patient_focus_id=body.patient_focus_id,
            awaiting_media=body.expects_media,
            date_order=body.date_order,
        )
    except upload.UploadError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except Exception:
        raise _storage_failed(db) from None
    if body.expects_media:
        # A new extension run: the previous ones are over even if the
        # extension never said so.
        media_upload.finalize_older_runs(db, user_id=user.id, exclude_id=imp.id)
    db.commit()
    db.refresh(imp)
    return _out(imp, received=[])


@router.get("/last", response_model=LastMessageOut)
def last_message(
    db: DbSession = Depends(get_db), _user: User = Depends(require_import_admin)
) -> LastMessageOut:
    last = media_upload.last_message_at(db)
    latest = db.scalars(
        select(WhatsappImport)
        .where(WhatsappImport.status == "done")
        .order_by(WhatsappImport.finished_at.desc().nulls_last(), WhatsappImport.id.desc())
        .limit(1)
    ).first()
    return LastMessageOut(
        last_message_at=to_local(last) if last else None,
        last_import_id=latest.id if latest else None,
        last_import_finished_at=latest.finished_at if latest else None,
    )


@router.get("/media-missing", response_model=MissingMediaOut)
def media_missing(
    import_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    kind: str | None = Query(default=None, pattern="^(" + "|".join(MESSAGE_MEDIA_KINDS) + ")$"),
    limit: int = Query(default=500, ge=1, le=media_upload.MAX_LIST),
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_import_admin),
) -> MissingMediaOut:
    items, has_more = media_upload.missing_media(
        db,
        import_id=import_id,
        since=to_local(since) if since else None,
        until=to_local(until) if until else None,
        kind=kind,
        limit=limit,
    )
    return MissingMediaOut(
        items=[
            MissingMediaItem(
                id=item.message.id,
                import_id=item.message.import_id,
                sent_at=to_local(item.message.sent_at),
                sender=item.message.sender,
                media_kind=item.message.media_kind,
                media_filename=item.message.media_filename,
                caption=(item.message.text or "")[: media_upload.CAPTION_CHARS],
                forwarded=item.message.forwarded,
                twin_index=item.twin_index,
                twin_count=item.twin_count,
                wa_key=item.message.wa_key,
            )
            for item in items
        ],
        has_more=has_more,
    )


@router.get("/{import_id}", response_model=WhatsappImportOut)
def get_import(
    import_id: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_import_admin),
    storage: Storage = Depends(storage_dep),
) -> WhatsappImportOut:
    imp = _get(db, import_id)
    received = None
    if imp.status == "uploading":
        try:
            received = upload.received_chunks(storage, imp)
        except Exception:
            raise _storage_failed(db) from None
    return _out(imp, received)


@router.put("/{import_id}/chunks/{index}", response_model=ChunkOut)
def put_chunk(
    import_id: int,
    index: int,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_import_admin),
    data: bytes = Depends(chunk_body),
    storage: Storage = Depends(storage_dep),
) -> ChunkOut:
    imp = _get(db, import_id)
    try:
        upload.put_chunk(storage, imp, index, data)
    except upload.UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except Exception:
        raise _storage_failed(db) from None
    return ChunkOut(index=index, size=len(data))


@router.post("/{import_id}/complete", response_model=WhatsappImportOut)
def complete_import(
    import_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_import_admin),
    storage: Storage = Depends(storage_dep),
) -> WhatsappImportOut:
    imp = _get(db, import_id, lock=True)
    try:
        upload.complete_upload(db, storage, imp, user_id=user.id)
    except upload.UploadError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except Exception:
        raise _storage_failed(db) from None
    db.commit()
    db.refresh(imp)
    return _out(imp)


@router.delete("/{import_id}", response_model=WhatsappImportOut)
def cancel_import(
    import_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_import_admin),
    storage: Storage = Depends(storage_dep),
) -> WhatsappImportOut:
    imp = _get(db, import_id, lock=True)
    try:
        upload.cancel_upload(db, storage, imp, user_id=user.id)
    except upload.UploadError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except Exception:
        raise _storage_failed(db) from None
    db.commit()
    db.refresh(imp)
    return _out(imp)


def _upload_error(exc: upload.UploadError) -> HTTPException:
    """The JSON error, plus a stable ``X-Error-Code`` header when the error
    has one (the extension branches on it, not on the Spanish text)."""
    headers = {"X-Error-Code": exc.code} if exc.code else None
    return HTTPException(status_code=exc.status, detail=str(exc), headers=headers)


def _media_cap_mb(settings: Settings) -> int:
    # Under the edge's 200 MB body limit, with room for the multipart envelope
    # (a larger body gets nginx's own 413 before reaching the API).
    return max(0, min(settings.whatsapp_media_max_mb, media_upload.MAX_UPLOAD_MB))


def _media_too_large(settings: Settings) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"El archivo supera el límite de {_media_cap_mb(settings)} MB.",
    )


@router.post(
    "/{import_id}/messages/{message_id}/media",
    response_model=MediaUploadOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_message_media(
    import_id: int,
    message_id: int,
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    wa_key: str | None = Form(default=None, pattern=media_upload.WA_KEY_PATTERN),
    db: DbSession = Depends(get_db),
    user: User = Depends(require_import_admin),
    storage: Storage = Depends(storage_dep),
    settings: Settings = Depends(get_settings),
) -> MediaUploadOut:
    """The file of stored message ``message_id`` (multipart field ``file``,
    optional field ``wa_key``: the WhatsApp Web bubble's ``data-id``),
    uploaded by the sync extension through its run ``import_id``."""
    cap = _media_cap_mb(settings) * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + 64 * 1024:
        raise _media_too_large(settings)
    # A share lock: completion (FOR UPDATE) waits for an upload in progress.
    imp = db.get(WhatsappImport, import_id, with_for_update={"read": True})
    if imp is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    try:
        media_upload.check_accepting(imp)
    except upload.UploadError as exc:
        db.rollback()
        raise _upload_error(exc) from None
    with tempfile.TemporaryDirectory(prefix="wa-media-") as tmp_dir:
        spooled = media_upload.spool(file.file, os.path.join(tmp_dir, "file"), cap)
        if spooled is None:
            db.rollback()
            raise _media_too_large(settings)
        if spooled.size == 0:
            db.rollback()
            raise HTTPException(status_code=400, detail="El archivo está vacío.")
        try:
            row, created = media_upload.attach_media(
                db,
                storage,
                imp,
                message_id,
                spooled,
                filename=file.filename,
                user_id=user.id,
                wa_key=wa_key,
            )
        except upload.UploadError as exc:
            db.rollback()
            raise _upload_error(exc) from None
        except IntegrityError as exc:
            # A concurrent upload recorded the same wa_key on another message
            # (the unique index): the same answer as the check before it.
            # Any other constraint is a real error.
            db.rollback()
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint != WA_KEY_INDEX:
                raise
            raise _upload_error(
                upload.UploadError(media_upload.WA_KEY_TAKEN, 409, media_upload.WA_KEY_CONFLICT)
            ) from None
        except Exception:
            raise _storage_failed(db) from None
    db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return MediaUploadOut(
        message_id=row.id,
        attachment_id=row.attachment_id,
        media_kind=row.media_kind,
        created=created,
    )


@router.post("/{import_id}/media/complete", response_model=WhatsappImportOut)
def complete_media(
    import_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_import_admin),
) -> WhatsappImportOut:
    imp = _get(db, import_id, lock=True)
    try:
        media_upload.complete_media(db, imp, user_id=user.id)
        media_upload.finalize_older_runs(db, user_id=user.id, exclude_id=imp.id)
    except upload.UploadError as exc:
        db.rollback()
        raise _upload_error(exc) from None
    db.commit()
    db.refresh(imp)
    return _out(imp)
