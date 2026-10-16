"""Files for stored messages, uploaded by the WhatsApp Web sync extension.

WhatsApp Web's «Exportar chat» has no media option: every file is
``<… omitted>`` in the text. The extension (DESIGN §6.8,
docs/extension-api.md) therefore runs in two steps, both through one
``whatsapp_imports`` row per run:

1. it uploads the text export as a ``daily`` import created with
   ``expects_media`` (→ ``awaiting_media``), through the normal chunked
   upload; the import job stores the new messages, without files;
2. once that import is ``done``, it asks which stored messages still lack
   their file (:func:`missing_media`), downloads some of them in WhatsApp
   Web and uploads each one for its message id (:func:`attach_media`, the
   file owned by the message, ``media_import_id`` = this run's import — the
   message may have been stored by an earlier run), then says it is done
   (:func:`complete_media`).

Completion (:func:`finalize`) queues the ``whatsapp_media`` job
(:func:`finish_media`), which runs the ``after_media`` hooks — transcription,
classification and lab extraction queue their jobs exactly as for files that
came inside an export — and clears ``awaiting_media``. For such a run those
hooks run only there, never at the end of the import (``process_import``),
so no file is handed over twice. The daily analysis of the run's import
waits while ``awaiting_media`` (bounded by ``MEDIA_WAIT``), so it sees the
transcripts and descriptions of the files uploaded in the same run. A file
for a message an *earlier* run stored is transcribed and classified too, but
that earlier day is not analyzed again.

A run the extension never completes (service worker killed, browser closed)
is finalized anyway, the same way: by the delayed ``whatsapp_media``
deadline job queued when its import finished (``MEDIA_WAIT`` later), by its
daily analysis once ``MEDIA_WAIT`` is over, and when the same admin starts
or completes a newer run (:func:`finalize_older_runs`). Uploads are refused
once a run is finalized or past ``MEDIA_WAIT``.

Privacy: senders, captions and file names are PHI; nothing here logs them
(ids and counts only).
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import IO

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.attachment import Attachment
from app.models.job import Job
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import audit, jobs
from app.services.attachments import PDF, FileType, sniff_media, store_file
from app.services.jobs import PermanentJobError
from app.services.storage import Storage
from app.services.whatsapp_import.process import (
    AFTER_MEDIA_HOOKS,
    MEDIA_JOB_KIND,
    MEDIA_WAIT,
    _media_counts_from_db,
    wants_media,
)
from app.services.whatsapp_import.upload import UploadError

logger = logging.getLogger("app.whatsapp_import")

READ_BLOCK = 1024 * 1024
CAPTION_CHARS = 200
DEFAULT_WINDOW = timedelta(days=30)
MAX_LIST = 1000
# Largest single file (MB) through the API: the edge allows 200 MB per
# request body and the multipart envelope needs a little room.
MAX_UPLOAD_MB = 199
# WhatsApp Web's message key: a bubble's ``data-id`` (docs/whatsapp-web-recon.md §2).
WA_KEY_PATTERN = r"^[0-9A-F]{32}$"

NOT_AWAITING = "Esta importación no espera archivos de la extensión."
NOT_READY = "La importación todavía no terminó de procesarse."
ALREADY_COMPLETE = "Ya se marcaron como completos los archivos de esta importación."
WINDOW_OVER = "Ya terminó el plazo para subir archivos a esta importación."
# Stable codes for the extension (``X-Error-Code``): the run takes no more
# files / a wrong bubble match.
RUN_CLOSED = "run_closed"
RUN_NOT_READY = "run_not_ready"
WA_KEY_CONFLICT = "wa_key_conflict"
WA_KEY_MISMATCH = "Ese mensaje ya se asoció a otro mensaje de WhatsApp."
WA_KEY_TAKEN = "Ese mensaje de WhatsApp ya se asoció a otro mensaje guardado."


def utcnow() -> datetime:
    return datetime.now(UTC)


def file_media_kind(file_type: FileType) -> str:
    """The ``whatsapp_messages.media_kind`` of a sniffed file type."""
    if file_type.mime == PDF.mime:
        return "pdf"
    major = file_type.mime.split("/", 1)[0]
    return major if major in {"image", "audio", "video"} else "other"


# ── Which messages still lack their file ─────────────────────────────────────


@dataclass
class MissingMedia:
    message: WhatsappMessage
    twin_index: int  # 0-based among stored media messages with the same key
    twin_count: int


def missing_media(
    db: DbSession,
    *,
    import_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    kind: str | None = None,
    limit: int = 500,
) -> tuple[list[MissingMedia], bool]:
    """Stored messages with media but no file, oldest first; returns
    ``(items, has_more)``. ``import_id``: only messages that import stored
    first. Without ``import_id`` or ``since``, the last ``DEFAULT_WINDOW``.

    *Twins* are stored media messages (with or without a file) sharing the
    ``sent_at`` **minute**, sender, text and ``media_kind``: WhatsApp Web
    shows minutes only, so the extension cannot tell them apart by those
    fields; each item says its position among them (``sent_at, id`` = chat
    order) and how many there are. The extension counts bubbles of the
    same kind, so the kind is part of the group: a photo and a voice note
    of one minute are never each other's twin."""
    query = select(WhatsappMessage).where(
        WhatsappMessage.media_kind.is_not(None), WhatsappMessage.attachment_id.is_(None)
    )
    if import_id is not None:
        query = query.where(WhatsappMessage.import_id == import_id)
    elif since is None:
        since = utcnow() - DEFAULT_WINDOW
    if since is not None:
        query = query.where(WhatsappMessage.sent_at >= since)
    if until is not None:
        query = query.where(WhatsappMessage.sent_at <= until)
    if kind is not None:
        query = query.where(WhatsappMessage.media_kind == kind)
    limit = max(1, min(MAX_LIST, limit))
    rows = list(
        db.scalars(query.order_by(WhatsappMessage.sent_at, WhatsappMessage.id).limit(limit + 1))
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return [], False

    # Compared as naive UTC minutes (any whole-minute offset, such as
    # Panamá's, gives the same minute).
    minute = func.date_trunc("minute", func.timezone("UTC", WhatsappMessage.sent_at))
    twins: dict[tuple, list[int]] = {}
    for twin_id, at_minute, sender, text, media_kind in db.execute(
        select(
            WhatsappMessage.id,
            minute,
            WhatsappMessage.sender,
            WhatsappMessage.text,
            WhatsappMessage.media_kind,
        )
        .where(
            minute.in_({_minute(m.sent_at) for m in rows}),
            WhatsappMessage.media_kind.is_not(None),
        )
        .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
    ):
        twins.setdefault((at_minute, sender, text, media_kind), []).append(twin_id)
    items = []
    for m in rows:
        group = twins.get((_minute(m.sent_at), m.sender, m.text, m.media_kind)) or [m.id]
        items.append(MissingMedia(m, group.index(m.id) if m.id in group else 0, len(group)))
    return items, has_more


def _minute(value: datetime) -> datetime:
    """``value`` as a naive UTC datetime truncated to the minute (the shape
    of ``date_trunc('minute', timezone('UTC', sent_at))``)."""
    return value.astimezone(UTC).replace(second=0, microsecond=0, tzinfo=None)


def last_message_at(db: DbSession) -> datetime | None:
    return db.scalar(select(func.max(WhatsappMessage.sent_at)))


# ── Uploading one file ──────────────────────────────────────────────────────


@dataclass
class SpooledFile:
    path: str
    size: int
    digest: str
    head: bytes


def spool(source: IO[bytes], path: str, cap: int) -> SpooledFile | None:
    """Copy ``source`` to ``path`` hashing it, reading at most ``cap`` bytes.
    None when it is larger than ``cap``."""
    digest = hashlib.sha256()
    size = 0
    head = b""
    with open(path, "wb") as out:
        while block := source.read(min(READ_BLOCK, cap + 1 - size)):
            size += len(block)
            if size > cap:
                return None
            if len(head) < 64:
                head += block[: 64 - len(head)]
            digest.update(block)
            out.write(block)
    return SpooledFile(path=path, size=size, digest=digest.hexdigest(), head=head)


def _audit_view(row: WhatsappMessage) -> dict:
    # The file link only: no text, sender or file name.
    return {
        "attachment_id": row.attachment_id,
        "media_import_id": row.media_import_id,
        "media_kind": row.media_kind,
        "wa_key": row.wa_key,
    }


def window_over(imp: WhatsappImport, *, now: datetime | None = None) -> bool:
    """Whether ``MEDIA_WAIT`` has passed since the import finished."""
    since = imp.finished_at
    return since is not None and (now or utcnow()) - since >= MEDIA_WAIT


def wa_key_owner(db: DbSession, wa_key: str, row_id: int) -> int | None:
    """Id of another message already matched to bubble ``wa_key``."""
    return db.scalar(
        select(WhatsappMessage.id).where(
            WhatsappMessage.wa_key == wa_key, WhatsappMessage.id != row_id
        )
    )


def check_accepting(imp: WhatsappImport) -> None:
    if not imp.awaiting_media and imp.media_completed_at is None:
        raise UploadError(NOT_AWAITING, 409, RUN_CLOSED)
    if imp.media_completed_at is not None:
        raise UploadError(ALREADY_COMPLETE, 409, RUN_CLOSED)
    if imp.status != "done":
        raise UploadError(NOT_READY, 409, RUN_NOT_READY)
    if window_over(imp):
        raise UploadError(WINDOW_OVER, 409, RUN_CLOSED)


def attach_media(
    db: DbSession,
    storage: Storage,
    imp: WhatsappImport,
    message_id: int,
    upload: SpooledFile,
    *,
    filename: str | None,
    user_id: int,
    wa_key: str | None = None,
) -> tuple[WhatsappMessage, bool]:
    """Store ``upload`` as the file of message ``message_id``, through import
    ``imp`` (the caller holds a share lock on it, so completion waits for
    this upload). ``wa_key``: the WhatsApp Web bubble (``data-id``) the
    extension matched; it is recorded on the message, and a key already
    recorded on *another* message, or a different key on this one, is a
    wrong match (409). Returns ``(message, created)``; ``created`` is False
    when the message already has these exact bytes (a retried upload). The
    caller commits."""
    check_accepting(imp)
    row = db.get(WhatsappMessage, message_id, with_for_update=True)
    if row is None:
        raise UploadError("Mensaje no encontrado.", 404)
    if row.media_kind is None:
        raise UploadError("Ese mensaje no tiene un archivo.", 409)
    if wa_key is not None:
        if row.wa_key is not None and row.wa_key != wa_key:
            raise UploadError(WA_KEY_MISMATCH, 409, WA_KEY_CONFLICT)
        if wa_key_owner(db, wa_key, row.id) is not None:
            raise UploadError(WA_KEY_TAKEN, 409, WA_KEY_CONFLICT)
    if row.attachment_id is not None:
        current = db.get(Attachment, row.attachment_id)
        if current is not None and current.sha256 == upload.digest:
            if wa_key is not None and row.wa_key is None:
                # A retry that now names the bubble: record (and audit) it.
                before = _audit_view(row)
                row.wa_key = wa_key
                db.flush()
                audit.record(
                    db,
                    user_id=user_id,
                    action=audit.ACTION_UPDATE,
                    entity="whatsapp_message",
                    entity_id=row.id,
                    before=before,
                    after=_audit_view(row),
                )
            return row, False
        raise UploadError("Ese mensaje ya tiene su archivo.", 409)
    file_type = sniff_media(upload.head)
    if file_type is None:
        raise UploadError("Tipo de archivo no reconocido.", 415)
    kind = file_media_kind(file_type)
    if row.media_kind != "other" and kind != row.media_kind:
        raise UploadError("El archivo no corresponde al tipo del mensaje.", 422)
    if not wants_media(imp.mode, kind):
        raise UploadError("Esta importación solo acepta PDF.", 422)

    before = _audit_view(row)
    attachment = store_file(
        db,
        storage,
        path=upload.path,
        digest=upload.digest,
        size=upload.size,
        file_type=file_type,
        filename=row.media_filename or filename,
        user_id=user_id,
        owner_type="whatsapp_message",
        owner_id=row.id,
    )
    row.attachment_id = attachment.id
    row.media_import_id = imp.id
    row.media_kind = kind
    if wa_key is not None:
        row.wa_key = wa_key
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity="whatsapp_message",
        entity_id=row.id,
        before=before,
        after=_audit_view(row),
    )
    return row, True


# ── Completion ────────────────────────────────────────────────────────────────

FINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


def files_added(db: DbSession, import_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(WhatsappMessage)
        .where(WhatsappMessage.media_import_id == import_id)
    )


def complete_media(db: DbSession, imp: WhatsappImport, *, user_id: int) -> Job | None:
    """The extension uploaded every file it will upload to ``imp`` (locked by
    the caller, who commits): :func:`finalize`, refusing an import that is no
    extension run or is still being processed. Idempotent."""
    if imp.media_completed_at is not None:
        return None
    if not imp.awaiting_media:
        raise UploadError(NOT_AWAITING, 409)
    if imp.status not in FINAL_STATUSES:
        raise UploadError(NOT_READY, 409)
    return finalize(db, imp, user_id=user_id)


def finalize(db: DbSession, imp: WhatsappImport, *, user_id: int | None = None) -> Job | None:
    """No more files for run ``imp`` (whoever decides it: the extension, the
    deadline, the analysis, a newer run). Sets ``media_completed_at`` and
    queues the ``whatsapp_media`` job when files were added; without files,
    clears ``awaiting_media`` at once. A no-op for an import that is not
    awaiting files, already finalized, or still being processed. The caller
    commits (and should hold the row lock)."""
    if not imp.awaiting_media or imp.media_completed_at is not None:
        return None
    if imp.status not in FINAL_STATUSES:
        return None
    before = audit.snapshot(imp)
    imp.media_completed_at = utcnow()
    job = None
    if imp.status == "done" and files_added(db, imp.id):
        job = jobs.enqueue(db, MEDIA_JOB_KIND, {"import_id": imp.id}, message="En cola")
    else:
        imp.awaiting_media = False
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
    return job


def finalize_older_runs(db: DbSession, *, user_id: int, exclude_id: int | None) -> int:
    """Finalize ``user_id``'s earlier runs still awaiting files (an extension
    run that never reached «complete»); returns how many. The caller commits."""
    query = (
        select(WhatsappImport)
        .where(
            WhatsappImport.uploaded_by == user_id,
            WhatsappImport.awaiting_media.is_(True),
            WhatsappImport.media_completed_at.is_(None),
            WhatsappImport.status.in_(FINAL_STATUSES),
        )
        .order_by(WhatsappImport.id)
        .with_for_update(skip_locked=True)
    )
    if exclude_id is not None:
        query = query.where(WhatsappImport.id != exclude_id)
    done = 0
    for imp in db.scalars(query):
        finalize(db, imp, user_id=user_id)
        done += 1
    if done:
        logger.info("whatsapp media: finalized %s stale run(s)", done)
    return done


def _lock_analysis(db: DbSession, import_id: int) -> None:
    """Wait for the daily analysis' per-import lock (``pipeline.lock_import``)
    so the two never rewrite ``stats`` over each other."""
    key = func.hashtextextended(f"analyze_import:{import_id}", 0)
    db.execute(select(func.pg_advisory_xact_lock(key)))


def finish_media(db: DbSession, import_id: int) -> WhatsappImport:
    """The ``whatsapp_media`` job: media stats, the ``after_media`` hooks and
    ``awaiting_media = False``, in one transaction (commits). Idempotent."""
    imp = db.get(WhatsappImport, import_id)
    if imp is None:
        raise PermanentJobError("La importación ya no existe.")
    if not imp.awaiting_media:
        return imp
    _lock_analysis(db, import_id)
    db.refresh(imp)
    if not imp.awaiting_media:
        db.commit()
        return imp
    before = audit.snapshot(imp)
    stats = dict(imp.stats or {})
    media = dict(stats.get("media") or {})
    for bucket, counts in _media_counts_from_db(db, imp).items():
        media[bucket] = dict(sorted(counts.items()))
    stats["media"] = media
    imp.stats = stats
    imp.awaiting_media = False
    db.flush()
    audit.record(
        db,
        user_id=None,
        action=audit.ACTION_UPDATE,
        entity="whatsapp_import",
        entity_id=imp.id,
        before=before,
        after=audit.snapshot(imp),
    )
    for hook in AFTER_MEDIA_HOOKS:
        hook(db, imp)
    db.commit()
    logger.info(
        "whatsapp media: import %s finished with %s file(s)", imp.id, files_added(db, imp.id)
    )
    return imp
