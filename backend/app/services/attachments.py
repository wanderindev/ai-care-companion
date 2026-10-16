"""Attachment uploads: content sniffing, SHA-256 dedupe, linking to owners.

The mime type is decided from the file's leading bytes, never from the
client's Content-Type or filename (phones send ``application/octet-stream``
for HEIC and voice notes, and a renamed file must not slip through).
"""

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.attachment import OWNER_TYPES, Attachment
from app.services import audit
from app.services.storage import Storage, storage_key


@dataclass(frozen=True)
class FileType:
    mime: str
    ext: str
    kind: str  # storage prefix: images / documents / audio


JPEG = FileType("image/jpeg", "jpg", "images")
PNG = FileType("image/png", "png", "images")
WEBP = FileType("image/webp", "webp", "images")
HEIC = FileType("image/heic", "heic", "images")
HEIF = FileType("image/heif", "heif", "images")
PDF = FileType("application/pdf", "pdf", "documents")
OGG = FileType("audio/ogg", "ogg", "audio")
MP3 = FileType("audio/mpeg", "mp3", "audio")
M4A = FileType("audio/mp4", "m4a", "audio")
AAC = FileType("audio/aac", "aac", "audio")

ALLOWED_TYPES = (JPEG, PNG, WEBP, HEIC, HEIF, PDF, OGG, MP3, M4A, AAC)

# Only stored from WhatsApp exports (``sniff_media``), never accepted by
# POST /attachments: videos are large and the bitácora does not show them.
MP4 = FileType("video/mp4", "mp4", "video")
MOV = FileType("video/quicktime", "mov", "video")
THREE_GP = FileType("video/3gpp", "3gp", "video")
# The WhatsApp export zip itself (``app.services.whatsapp_import``).
ZIP = FileType("application/zip", "zip", "archives")
# Spreadsheets the chat generates (``generate_xlsx``); never accepted by
# POST /attachments (an .xlsx is a zip, so ``sniff`` cannot tell it apart).
XLSX = FileType(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx", "documents"
)

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"}
_HEIF_BRANDS = {b"mif1", b"msf1"}
_M4A_BRANDS = {b"M4A ", b"M4B "}
_MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"MSNV"}
_3GP_BRANDS = {b"3gp4", b"3gp5", b"3gp6", b"3g2a"}


def sniff(data: bytes) -> FileType | None:
    """The allowed file type ``data`` starts with, or None."""
    head = data[:64]
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return WEBP
    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"OggS"):
        return OGG
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return HEIC
        if brand in _HEIF_BRANDS:
            return HEIF
        if brand in _M4A_BRANDS:
            return M4A
        return None
    if head.startswith(b"ID3"):
        return MP3
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # MPEG audio frame sync. Layer bits 00 = AAC in an ADTS stream.
        return AAC if (head[1] >> 1) & 0x03 == 0 else MP3
    return None


def sniff_media(data: bytes) -> FileType | None:
    """``sniff`` plus the video containers WhatsApp sends (mp4, mov, 3gp)."""
    head = data[:64]
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _MP4_BRANDS:
            return MP4
        if brand == b"qt  ":
            return MOV
        if brand in _3GP_BRANDS:
            return THREE_GP
    return sniff(data)


def clean_filename(name: str | None, file_type: FileType) -> str:
    """Display filename: path parts and control characters removed, sane length."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    if not base:
        base = f"archivo.{file_type.ext}"
    if len(base) > 200:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 200 - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else base[:200]
    return base


def _object_key(db: DbSession, digest: str, file_type: FileType) -> tuple[str, bool]:
    """Key for content ``digest``: an existing row's key (dedupe) or a new one.

    Returns ``(key, reused)``. Reusing the key of any earlier row with the same
    SHA-256 means a re-upload in a later year still points at the original
    object.
    """
    existing_key = db.scalar(
        select(Attachment.storage_key)
        .where(Attachment.sha256 == digest)
        .order_by(Attachment.id)
        .limit(1)
    )
    if existing_key is not None:
        return existing_key, True
    return storage_key(file_type.kind, digest, file_type.ext), False


def _add_row(
    db: DbSession,
    *,
    key: str,
    digest: str,
    size: int,
    file_type: FileType,
    filename: str | None,
    user_id: int | None,
    owner_type: str | None = None,
    owner_id: int | None = None,
) -> Attachment:
    attachment = Attachment(
        kind=file_type.kind,
        storage_key=key,
        filename=clean_filename(filename, file_type),
        mime=file_type.mime,
        size=size,
        sha256=digest,
        uploaded_by=user_id,
        owner_type=owner_type,
        owner_id=owner_id,
    )
    db.add(attachment)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity="attachment",
        entity_id=attachment.id,
        after=audit.snapshot(attachment),
    )
    return attachment


def store_upload(
    db: DbSession,
    storage: Storage,
    *,
    data: bytes,
    file_type: FileType,
    filename: str | None,
    user_id: int | None,
) -> Attachment:
    """Upload (unless the same bytes are already stored) and add an unlinked row.

    The caller commits. If no row with the same SHA-256 exists the object may
    still be there (e.g. a row rolled back after the upload), which ``exists``
    catches.
    """
    digest = hashlib.sha256(data).hexdigest()
    key, reused = _object_key(db, digest, file_type)
    if not reused and not storage.exists(key):
        storage.put(key, data, file_type.mime)
    return _add_row(
        db,
        key=key,
        digest=digest,
        size=len(data),
        file_type=file_type,
        filename=filename,
        user_id=user_id,
    )


def store_file(
    db: DbSession,
    storage: Storage,
    *,
    path: str,
    digest: str,
    size: int,
    file_type: FileType,
    filename: str | None,
    user_id: int | None,
    owner_type: str | None = None,
    owner_id: int | None = None,
) -> Attachment:
    """``store_upload`` for a file on disk whose SHA-256 the caller computed while
    writing it (large files never sit in memory). Optionally owned at once."""
    if owner_type is not None and owner_type not in OWNER_TYPES:
        raise AttachmentLinkError(f"unknown owner_type {owner_type!r}")
    key, reused = _object_key(db, digest, file_type)
    if not reused and not storage.exists(key):
        storage.upload_file(key, path, file_type.mime)
    return _add_row(
        db,
        key=key,
        digest=digest,
        size=size,
        file_type=file_type,
        filename=filename,
        user_id=user_id,
        owner_type=owner_type,
        owner_id=owner_id,
    )


def store_existing_object(
    db: DbSession,
    storage: Storage,
    *,
    key: str,
    digest: str,
    size: int,
    file_type: FileType,
    filename: str | None,
    user_id: int | None,
    owner_type: str,
    owner_id: int,
) -> Attachment:
    """Add a row for an object already in the bucket at ``key`` (e.g. a finished
    multipart upload). When the same bytes are already stored the earlier key
    is used and the object at ``key`` is deleted (no row refers to it)."""
    existing, reused = _object_key(db, digest, file_type)
    if reused and existing != key:
        storage.delete(key)
        key = existing
    return _add_row(
        db,
        key=key,
        digest=digest,
        size=size,
        file_type=file_type,
        filename=filename,
        user_id=user_id,
        owner_type=owner_type,
        owner_id=owner_id,
    )


class AttachmentLinkError(ValueError):
    pass


def link_attachments(
    db: DbSession,
    attachment_ids: list[int],
    *,
    owner_type: str,
    owner_id: int,
    user_id: int | None,
) -> list[Attachment]:
    """Attach uploaded files to their owner (e.g. a log entry when it is saved).

    Only unlinked attachments (or ones already linked to this same owner) can
    be linked; anything else raises ``AttachmentLinkError`` so the caller can
    answer 400/409. The caller commits.
    """
    if owner_type not in OWNER_TYPES:
        raise AttachmentLinkError(f"unknown owner_type {owner_type!r}")
    ids = list(dict.fromkeys(attachment_ids))
    rows = list(db.scalars(select(Attachment).where(Attachment.id.in_(ids)).with_for_update()))
    if len(rows) != len(ids):
        raise AttachmentLinkError("unknown attachment id")
    for row in rows:
        if row.owner_type is None:
            before = audit.snapshot(row)
            row.owner_type, row.owner_id = owner_type, owner_id
            audit.record(
                db,
                user_id=user_id,
                action=audit.ACTION_LINK,
                entity="attachment",
                entity_id=row.id,
                before=before,
                after=audit.snapshot(row),
            )
        elif (row.owner_type, row.owner_id) != (owner_type, owner_id):
            raise AttachmentLinkError(f"attachment {row.id} already belongs to another owner")
    db.flush()
    return rows


def unlink_attachments(
    db: DbSession,
    *,
    owner_type: str,
    owner_id: int,
    keep_ids: list[int] | tuple[int, ...] = (),
    user_id: int | None,
) -> list[Attachment]:
    """Detach an owner's files except ``keep_ids`` (owner back to NULL, audited).

    The rows and stored objects stay: an unlinked attachment can be linked
    again, and the audit log keeps who removed it. Returns the unlinked rows;
    the caller commits.
    """
    stmt = select(Attachment).where(
        Attachment.owner_type == owner_type, Attachment.owner_id == owner_id
    )
    if keep_ids:
        stmt = stmt.where(Attachment.id.not_in(list(keep_ids)))
    rows = list(db.scalars(stmt.order_by(Attachment.id).with_for_update()))
    for row in rows:
        before = audit.snapshot(row)
        row.owner_type, row.owner_id = None, None
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_UNLINK,
            entity="attachment",
            entity_id=row.id,
            before=before,
            after=audit.snapshot(row),
        )
    db.flush()
    return rows


def copy_attachments(
    db: DbSession,
    sources: list[Attachment],
    *,
    owner_type: str,
    owner_id: int,
    user_id: int | None,
) -> list[Attachment]:
    """Give an owner its own row for each of ``sources``' files (audited).

    An attachment has a single owner, so a file that already belongs to
    something else (a WhatsApp message's photo that an approved suggestion
    turns into a bitácora entry) is not re-linked, which would take it away
    from its message: a new row points at the **same stored object** (same
    ``storage_key`` and SHA-256; rows sharing an object are the normal dedupe
    case, and no object is deleted while a row references it). Files the
    owner already has (same SHA-256) are skipped, so a repeat is harmless.
    Returns the new rows; the caller commits.
    """
    if owner_type not in OWNER_TYPES:
        raise AttachmentLinkError(f"unknown owner_type {owner_type!r}")
    have = set(
        db.scalars(
            select(Attachment.sha256).where(
                Attachment.owner_type == owner_type, Attachment.owner_id == owner_id
            )
        )
    )
    created: list[Attachment] = []
    for source in sources:
        if source.sha256 in have:
            continue
        have.add(source.sha256)
        row = Attachment(
            kind=source.kind,
            storage_key=source.storage_key,
            filename=source.filename,
            mime=source.mime,
            size=source.size,
            sha256=source.sha256,
            uploaded_by=user_id,
            owner_type=owner_type,
            owner_id=owner_id,
        )
        db.add(row)
        db.flush()
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_CREATE,
            entity="attachment",
            entity_id=row.id,
            after={**audit.snapshot(row), "copied_from": source.id},
        )
        created.append(row)
    return created
