"""The import job: stored zip → messages (deduped) + media attachments.

``process_import`` runs in the worker (``jobs.py``). Steps, each safe to
repeat (a retry or a run after a worker crash starts again from the top):

1. Stream the zip from Spaces to a temp file (hashing it on the way) and
   parse it (``app.services.whatsapp.parse_export``). A file that is not a
   WhatsApp export fails the import for good. A day/month tie is broken by
   the uploader's ``stats.date_order_hint`` (the sync extension's
   ``date_order``) and the newest message stored by other imports (the
   parser's ``floor``). A ``daily`` import whose new messages are mostly
   day/month-swapped twins of stored ones (``swapped_twins``, same sender
   and minute) fails for good before anything is stored (#117).
2. Keep the zip as an attachment owned by the import (deduped by SHA-256:
   re-uploading the same zip keeps one object).
3. Insert the non-system messages whose ``dedupe_hash`` is not stored yet
   (``ON CONFLICT DO NOTHING``, so concurrent imports cannot duplicate).
4. For every message whose file is in the zip and that the mode wants
   (``labs_only``: PDFs only; ``backfill``/``daily``: every kind), when the
   stored message has no file yet, stream the member to a temp file with a
   byte cap, sniff its type and store it (SHA-256 dedupe) as an attachment
   owned by the message. This also gives files to messages first stored from
   an export *without* media ("added to existing").
5. Record ``range_from``/``range_to`` and ``stats``, mark the import done and
   run the ``after_import`` hooks (lab extraction registers one in #13).

``stats`` (jsonb)::

    {"messages": {"total", "new", "duplicate", "system_skipped"},
     "media": {"<bucket>": {"<kind>": n}},  # buckets below
     "unparsed_lines": n, "unreferenced_files": n}

Media buckets: ``uploaded`` (file stored for a message new in this import),
``added_to_existing`` (file stored for a message an earlier import had stored
without it), ``already_stored`` (the message already had its file),
``skipped_mode`` (kind not wanted in this mode), ``omitted`` (exported
without media), ``missing`` (named in the chat but not in the zip),
``too_large`` (over ``WHATSAPP_MEDIA_MAX_MB``), ``unsupported`` (not a
known image/PDF/audio/video type, or unreadable in the zip). System messages
("X added Y", encryption notices) are not stored; they are counted in
``system_skipped``.

Privacy: progress messages and errors are generic; nothing here logs message
text, senders or file names.
"""

import hashlib
import logging
import os
import tempfile
import time
import zipfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings
from app.models.attachment import Attachment
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import audit, jobs
from app.services.attachments import ZIP, sniff_media, store_existing_object, store_file
from app.services.jobs import PermanentJobError
from app.services.storage import Storage
from app.services.whatsapp import PANAMA_TZ, WaExportError, WaMessage, parse_export
from app.services.whatsapp_import.date_order import swap_day_month

logger = logging.getLogger("app.whatsapp_import")

Progress = Callable[[int, str | None], None]
AfterImportHook = Callable[[DbSession, WhatsappImport], None]

# Hooks run in the job's transaction once an import is done, e.g. lab
# extraction enqueueing a job for the new PDFs (``pdf_messages``).
AFTER_IMPORT_HOOKS: list[AfterImportHook] = []

READ_BLOCK = 1024 * 1024
BATCH = 500
COMMIT_EVERY_FILES = 20
PROGRESS_EVERY_SECONDS = 5.0
LABS_ONLY_KINDS = frozenset({"pdf"})
MEDIA_BUCKETS = (
    "uploaded",
    "added_to_existing",
    "already_stored",
    "skipped_mode",
    "omitted",
    "missing",
    "too_large",
    "unsupported",
)


# Hooks that only look at the files an import stored (``media_import_id``):
# they run again, in the ``whatsapp_media`` job, once the sync extension has
# uploaded files to an import after it finished (``media_upload``). Register
# with both decorators; a hook must then find only files not handled yet
# (true for media keyed on ``media_import_id``: an extension import's
# messages have no files when the import finishes).
AFTER_MEDIA_HOOKS: list[AfterImportHook] = []

# Sync extension runs (``media_upload``): the job that runs the
# ``after_media`` hooks, and how long after the import finished the run may
# still receive files (afterwards it is finalized without the extension).
MEDIA_JOB_KIND = "whatsapp_media"
MEDIA_WAIT = timedelta(minutes=90)

# A `daily` import whose new messages are mostly (more than half, and at
# least this many) day/month-swapped twins of stored ones is refused (#117).
SWAP_MIN = 2


def after_import(fn: AfterImportHook) -> AfterImportHook:
    """Register ``fn(db, imp)`` to run when an import finishes (same transaction)."""
    if fn not in AFTER_IMPORT_HOOKS:
        AFTER_IMPORT_HOOKS.append(fn)
    return fn


def after_media(fn: AfterImportHook) -> AfterImportHook:
    """Register ``fn(db, imp)`` to run when the extension's media uploads to an
    import are complete (``media_upload.finish_media``, same transaction)."""
    if fn not in AFTER_MEDIA_HOOKS:
        AFTER_MEDIA_HOOKS.append(fn)
    return fn


def pdf_messages(db: DbSession, import_id: int) -> list[WhatsappMessage]:
    """Messages whose PDF file was stored by import ``import_id`` (new files only:
    a file another import already stored is not returned again)."""
    return list(
        db.scalars(
            select(WhatsappMessage)
            .join(Attachment, Attachment.id == WhatsappMessage.attachment_id)
            .where(
                WhatsappMessage.media_import_id == import_id,
                Attachment.mime == "application/pdf",
            )
            .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
        )
    )


def wants_media(mode: str, kind: str | None) -> bool:
    if mode == "labs_only":
        return kind in LABS_ONLY_KINDS
    return True


class _Throttle:
    def __init__(self, progress: Progress):
        self._progress = progress
        self._last = 0.0

    def __call__(self, percent: int, message: str | None, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last >= PROGRESS_EVERY_SECONDS:
            self._last = now
            self._progress(percent, message)


def _download(storage: Storage, key: str, path: str, size: int, progress: _Throttle) -> str:
    """Stream the object to ``path``; returns its SHA-256."""
    digest = hashlib.sha256()
    done = 0
    with open(path, "wb") as out:
        for block in storage.iter_object(key):
            out.write(block)
            digest.update(block)
            done += len(block)
            progress(min(10, 10 * done // max(size, 1)), "Descargando el archivo")
    return digest.hexdigest()


def _extract(archive: zipfile.ZipFile, member: str, path: str, cap: int):
    """Stream one member to ``path`` reading at most ``cap`` bytes.

    Returns ``(size, sha256, head)``, ``"too_large"`` or ``"unsupported"``
    (unreadable: corrupt, encrypted, unknown compression).
    """
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with archive.open(member) as src, open(path, "wb") as out:
            while block := src.read(min(READ_BLOCK, cap + 1 - size)):
                size += len(block)
                if size > cap:
                    return "too_large"
                if len(head) < 64:
                    head += block[: 64 - len(head)]
                digest.update(block)
                out.write(block)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError):
        return "unsupported"
    if size == 0:
        return "unsupported"
    return size, digest.hexdigest(), head


def _store_zip(
    db: DbSession, storage: Storage, imp: WhatsappImport, path: str, digest: str
) -> None:
    attachment = store_existing_object(
        db,
        storage,
        key=imp.staging_key,
        digest=digest,
        size=os.path.getsize(path),
        file_type=ZIP,
        filename=imp.filename,
        user_id=imp.uploaded_by,
        owner_type="whatsapp_import",
        owner_id=imp.id,
    )
    imp.attachment_id = attachment.id
    imp.staging_key = None


def _insert_messages(
    db: DbSession, imp: WhatsappImport, messages: list[WaMessage]
) -> dict[str, tuple[int, int | None, int | None]]:
    """Insert unseen messages; returns ``hash -> (id, attachment_id, media_import_id)``
    for every message of the export."""
    stored: dict[str, tuple[int, int | None, int | None]] = {}
    for start in range(0, len(messages), BATCH):
        batch = messages[start : start + BATCH]
        rows = [
            {
                "import_id": imp.id,
                "sent_at": m.sent_at,
                "sender": m.sender[:200] if m.sender else None,
                "text": m.text,
                "forwarded": m.forwarded,
                "media_kind": m.media_kind,
                "media_filename": m.media_filename[:255] if m.media_filename else None,
                "dedupe_hash": m.dedupe_hash,
            }
            for m in batch
        ]
        db.execute(
            insert(WhatsappMessage)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["dedupe_hash"])
        )
        found = db.execute(
            select(
                WhatsappMessage.dedupe_hash,
                WhatsappMessage.id,
                WhatsappMessage.attachment_id,
                WhatsappMessage.media_import_id,
            ).where(WhatsappMessage.dedupe_hash.in_([m.dedupe_hash for m in batch]))
        )
        for row in found:
            stored[row.dedupe_hash] = (row.id, row.attachment_id, row.media_import_id)
    return stored


def _attach_media(
    db: DbSession,
    storage: Storage,
    settings: Settings,
    imp: WhatsappImport,
    zip_path: str,
    messages: list[WaMessage],
    stored: dict[str, tuple[int, int | None, int | None]],
    tmp_dir: str,
    progress: _Throttle,
) -> dict[str, Counter]:
    counts: dict[str, Counter] = defaultdict(Counter)
    cap = settings.whatsapp_media_max_mb * 1024 * 1024
    with_media = [m for m in messages if m.media_kind]
    todo = []
    for message in with_media:
        kind = message.media_kind
        if message.media_omitted:
            counts["omitted"][kind] += 1
        elif message.media_member is None:
            counts["missing"][kind] += 1
        elif not wants_media(imp.mode, kind):
            counts["skipped_mode"][kind] += 1
        else:
            _id, attachment_id, media_import_id = stored[message.dedupe_hash]
            if attachment_id is None:
                todo.append(message)
            elif media_import_id != imp.id:
                counts["already_stored"][kind] += 1
            # else: stored by an earlier attempt of this same import; counted
            # from the database at the end.

    member_path = os.path.join(tmp_dir, "member")
    with zipfile.ZipFile(zip_path) as archive:
        for done, message in enumerate(todo, start=1):
            kind = message.media_kind
            result = _extract(archive, message.media_member, member_path, cap)
            if isinstance(result, str):
                counts[result][kind] += 1
                continue
            size, digest, head = result
            file_type = sniff_media(head)
            if file_type is None:
                counts["unsupported"][kind] += 1
                continue
            message_id = stored[message.dedupe_hash][0]
            row = db.get(WhatsappMessage, message_id, with_for_update=True)
            if row is None or row.attachment_id is not None:  # a concurrent import
                counts["already_stored"][kind] += 1
                continue
            attachment = store_file(
                db,
                storage,
                path=member_path,
                digest=digest,
                size=size,
                file_type=file_type,
                filename=message.media_member.rsplit("/", 1)[-1],
                user_id=imp.uploaded_by,
                owner_type="whatsapp_message",
                owner_id=row.id,
            )
            row.attachment_id = attachment.id
            row.media_import_id = imp.id
            # A message stored from an export without media may only know a
            # generic kind ("<Media omitted>") and no file name.
            row.media_kind = kind
            row.media_filename = (message.media_filename or "")[:255] or row.media_filename
            if done % COMMIT_EVERY_FILES == 0:
                db.commit()
            progress(
                30 + 65 * done // max(len(todo), 1),
                f"Guardando archivos: {done} de {len(todo)}",
            )
    db.commit()
    return counts


def _media_counts_from_db(db: DbSession, imp: WhatsappImport) -> dict[str, Counter]:
    """Files this import stored, split by new vs earlier message (retry-safe)."""
    counts: dict[str, Counter] = defaultdict(Counter)
    is_new = case((WhatsappMessage.import_id == imp.id, True), else_=False)
    rows = db.execute(
        select(is_new, WhatsappMessage.media_kind, func.count())
        .where(WhatsappMessage.media_import_id == imp.id)
        .group_by(is_new, WhatsappMessage.media_kind)
    )
    for new, kind, n in rows:
        counts["uploaded" if new else "added_to_existing"][kind or "other"] += n
    return counts


def _stats(
    db: DbSession,
    imp: WhatsappImport,
    report,
    messages: list[WaMessage],
    system_skipped: int,
    counts: dict[str, Counter],
) -> dict[str, Any]:
    new = db.scalar(
        select(func.count()).select_from(WhatsappMessage).where(WhatsappMessage.import_id == imp.id)
    )
    counts = {**counts, **_media_counts_from_db(db, imp)}
    return {
        "messages": {
            "total": len(messages),
            "new": new,
            "duplicate": len(messages) - new,
            "system_skipped": system_skipped,
        },
        "media": {
            bucket: dict(sorted(counts[bucket].items()))
            for bucket in MEDIA_BUCKETS
            if counts.get(bucket)
        },
        "unparsed_lines": len(report.chat.unparsed_lines),
        "unreferenced_files": report.unreferenced_media,
    }


def _fail(db: DbSession, storage: Storage, imp: WhatsappImport) -> None:
    """Mark the import failed for good and drop the uploaded zip."""
    db.rollback()
    if imp.staging_key:
        try:
            storage.delete(imp.staging_key)
        except Exception:
            logger.warning("could not delete the staging zip of import %s", imp.id)
        imp.staging_key = None
    imp.status = "failed"
    imp.finished_at = datetime.now(UTC)
    db.commit()


def _floor(db: DbSession, imp: WhatsappImport) -> datetime | None:
    """The newest message stored by another import (a retry must not use its
    own earlier attempt's rows): the parser's day/month tie-break floor."""
    return db.scalar(
        select(func.max(WhatsappMessage.sent_at)).where(WhatsappMessage.import_id != imp.id)
    )


def swapped_twins(db: DbSession, imp: WhatsappImport, messages: list[WaMessage]) -> tuple[int, int]:
    """``(new, swapped)``: how many of ``messages`` are not stored yet (by
    another import), and how many of those have a stored message from the
    same sender at the same minute on the date with day and month swapped
    -- the sign of a file read in the wrong day/month order (#117)."""
    known: set[str] = set()
    for start in range(0, len(messages), BATCH):
        batch = [m.dedupe_hash for m in messages[start : start + BATCH]]
        known.update(
            db.scalars(
                select(WhatsappMessage.dedupe_hash).where(
                    WhatsappMessage.dedupe_hash.in_(batch), WhatsappMessage.import_id != imp.id
                )
            )
        )
    new = [m for m in messages if m.dedupe_hash not in known]
    wanted: dict[tuple, int] = Counter()
    for message in new:
        local = message.sent_at.astimezone(PANAMA_TZ)
        swapped = swap_day_month(local.date())
        if swapped is not None:
            wanted[((message.sender or "")[:200], swapped, local.hour, local.minute)] += 1
    if not wanted:
        return len(new), 0
    days = sorted({key[1] for key in wanted})
    ranges = [
        and_(
            WhatsappMessage.sent_at >= datetime.combine(day, dtime(), PANAMA_TZ),
            WhatsappMessage.sent_at < datetime.combine(day + timedelta(days=1), dtime(), PANAMA_TZ),
        )
        for day in days
    ]
    stored = set()
    for sender, sent_at in db.execute(
        select(WhatsappMessage.sender, WhatsappMessage.sent_at).where(
            WhatsappMessage.import_id != imp.id, or_(*ranges)
        )
    ):
        local = sent_at.astimezone(PANAMA_TZ)
        stored.add((sender or "", local.date(), local.hour, local.minute))
    return len(new), sum(n for key, n in wanted.items() if key in stored)


def process_import(
    db: DbSession,
    storage: Storage,
    settings: Settings,
    import_id: int,
    progress: Progress,
) -> WhatsappImport:
    """Run (or re-run) import ``import_id``. Commits along the way."""
    imp = db.get(WhatsappImport, import_id)
    if imp is None:
        raise PermanentJobError("La importación ya no existe.")
    if imp.status in {"done", "failed", "cancelled"}:
        return imp
    if imp.status == "uploading":
        raise PermanentJobError("La importación no terminó de subirse.")
    throttle = _Throttle(progress)
    before = audit.snapshot(imp)
    imp.status = "processing"
    db.commit()

    with tempfile.TemporaryDirectory(prefix="wa-import-") as tmp_dir:
        zip_path = os.path.join(tmp_dir, "export.zip")
        key = imp.staging_key
        if key is None:
            attachment = db.get(Attachment, imp.attachment_id) if imp.attachment_id else None
            if attachment is None:
                _fail(db, storage, imp)
                raise PermanentJobError("No se encontró el archivo de la importación.")
            key = attachment.storage_key
        throttle(0, "Descargando el archivo", force=True)
        digest = _download(storage, key, zip_path, imp.size, throttle)

        throttle(10, "Leyendo el chat", force=True)
        hint = (imp.stats or {}).get("date_order_hint")
        try:
            report = parse_export(
                zip_path,
                reference=imp.created_at,
                date_order=hint,
                floor=_floor(db, imp),
            )
        except WaExportError:
            _fail(db, storage, imp)
            raise PermanentJobError(
                "El archivo no es una exportación de chat de WhatsApp válida."
            ) from None
        if imp.mode == "daily":
            new, swapped = swapped_twins(db, imp, [m for m in report.messages if not m.is_system])
            if swapped >= SWAP_MIN and swapped * 2 > new:
                logger.warning(
                    "import %s refused: %s of %s new messages are day/month-swapped twins",
                    imp.id,
                    swapped,
                    new,
                )
                _fail(db, storage, imp)
                raise PermanentJobError(
                    "El orden de las fechas del archivo es ambiguo: la mayoría de los "
                    "mensajes nuevos repiten mensajes guardados con el día y el mes "
                    "invertidos. No se guardó nada."
                )
        if imp.staging_key is not None:
            _store_zip(db, storage, imp, zip_path, digest)
            db.commit()

        everything = report.messages
        messages = [m for m in everything if not m.is_system]
        system_skipped = len(everything) - len(messages)
        throttle(15, "Guardando mensajes", force=True)
        stored = _insert_messages(db, imp, messages)
        db.commit()

        throttle(30, "Guardando archivos", force=True)
        counts = _attach_media(
            db, storage, settings, imp, zip_path, messages, stored, tmp_dir, throttle
        )

    stamps = [m.sent_at for m in everything]
    imp.range_from = min(stamps) if stamps else None
    imp.range_to = max(stamps) if stamps else None
    imp.stats = _stats(db, imp, report, messages, system_skipped, counts)
    imp.stats["date_order"] = report.chat.date_order
    if hint:
        imp.stats["date_order_hint"] = hint
    imp.status = "done"
    imp.finished_at = datetime.now(UTC)
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
    for hook in AFTER_IMPORT_HOOKS:
        if imp.awaiting_media and hook in AFTER_MEDIA_HOOKS:
            continue  # an extension run: they run once, at media_upload.finish_media
        hook(db, imp)
    if imp.awaiting_media:
        # The deadline: finalizes the run even if the extension never says
        # it is done (``media_upload.finalize``; a no-op once completed).
        jobs.enqueue(
            db,
            MEDIA_JOB_KIND,
            {"import_id": imp.id, "deadline": True},
            message="En cola",
            delay=MEDIA_WAIT,
        )
    db.commit()
    throttle(100, "Listo", force=True)
    return imp
