import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session as DbSession

from app.api.deps import require_user
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models.attachment import Attachment
from app.models.user import User
from app.schemas.attachments import AttachmentOut, AttachmentUrlOut
from app.services.attachments import sniff, store_upload
from app.services.storage import Storage, StorageNotConfigured, get_storage

logger = logging.getLogger("app.attachments")

router = APIRouter(prefix="/attachments", tags=["attachments"])

UNSUPPORTED_MESSAGE = "Tipo de archivo no permitido. Sube una foto, un PDF o un audio."
STORAGE_UNAVAILABLE = "El almacenamiento de archivos no está disponible."
# Room for the multipart envelope around the file itself.
_MULTIPART_OVERHEAD = 64 * 1024


def storage_dep() -> Storage:
    try:
        return get_storage()
    except StorageNotConfigured:
        logger.warning("Attachment storage is not configured (DO_SPACES_KEY/SECRET)")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORAGE_UNAVAILABLE
        ) from None


def _too_large(settings: Settings) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"El archivo supera el límite de {settings.attachment_max_mb} MB.",
    )


@router.post("", response_model=AttachmentOut, status_code=status.HTTP_201_CREATED)
def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
    storage: Storage = Depends(storage_dep),
    settings: Settings = Depends(get_settings),
) -> Attachment:
    """Upload one file (multipart field ``file``). The new attachment is not
    linked to anything yet; the owner (e.g. a log entry) links it on save."""
    max_bytes = settings.attachment_max_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + _MULTIPART_OVERHEAD:
        raise _too_large(settings)

    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise _too_large(settings)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo está vacío."
        )
    file_type = sniff(data)
    if file_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=UNSUPPORTED_MESSAGE
        )

    try:
        attachment = store_upload(
            db, storage, data=data, file_type=file_type, filename=file.filename, user_id=user.id
        )
    except Exception:
        db.rollback()
        # No filename or content in the log (may be medical).
        logger.exception("Attachment upload to storage failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORAGE_UNAVAILABLE
        ) from None
    db.commit()
    db.refresh(attachment)
    return attachment


@router.get("/{attachment_id}/url", response_model=AttachmentUrlOut)
def attachment_url(
    attachment_id: int,
    download: bool = False,
    db: DbSession = Depends(get_db),
    _user: User = Depends(require_user),
    storage: Storage = Depends(storage_dep),
) -> AttachmentUrlOut:
    """A short-lived (≤ 10 min) presigned GET URL for the file."""
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")
    url = storage.presigned_get(
        attachment.storage_key,
        filename=attachment.filename,
        content_type=attachment.mime,
        download=download,
    )
    return AttachmentUrlOut(url=url, expires_in=storage.url_ttl)
