"""Worker side of voice-note transcription (listed in ``app.worker.HANDLER_MODULES``).

- ``after_import`` hook ``enqueue_transcriptions``: when a ``daily`` or
  ``backfill`` import finishes (never ``labs_only``, which stores no audio),
  every audio file the import stored is matched by SHA-256 against
  ``audio_transcripts``: a known transcript is copied to the message at once;
  otherwise one ``transcribe_audio`` job is queued per distinct hash. It only
  enqueues (in the import's transaction): no provider call there.
- ``transcribe_audio`` handler. Payload ``{"attachment_id": id}``. Under a
  per-hash advisory lock it re-checks ``audio_transcripts`` (a second job for
  the same file only copies the text), then sends the file once and stores
  the transcript in ``audio_transcripts`` and in
  ``whatsapp_messages.transcript`` of every message whose file has that hash.

Failures: no key / unknown provider / a key the provider refuses → the job
fails at once with a generic message and nothing is recorded (re-run it once
the settings are fixed). A file over ``TRANSCRIPTION_MAX_MB``, of a type the
provider does not take, or refused by it (400/413/415) is recorded as
``failed`` with a short code, so it is never sent again, and fails the job
without retrying. Timeouts, network errors, 429 and 5xx retry with the job's
backoff. Logs carry ids, sizes, durations and cost only — never the
transcript, the hints or file names.
"""

import logging
import time
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.config import get_settings
from app.models.attachment import Attachment
from app.models.transcription import AudioTranscript
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.storage import StorageNotConfigured, get_storage
from app.services.transcription import hints
from app.services.transcription.provider import (
    TranscriptionError,
    TranscriptionNotConfigured,
    get_provider,
    upload_format,
)
from app.services.whatsapp_import import after_import, after_media

JOB_KIND = "transcribe_audio"
TRANSCRIBE_MODES = frozenset({"daily", "backfill"})
AUDIO_KIND = "audio"  # attachments.kind (storage prefix) of audio files
LANGUAGE = "es"

NOT_CONFIGURED = "La transcripción automática no está configurada."
FAILED_MESSAGES = {
    "too_large": "La nota de voz es demasiado grande para transcribirla automáticamente.",
    "unsupported": "Este formato de audio no se puede transcribir automáticamente.",
}
FAILED_DEFAULT = "No se pudo transcribir la nota de voz."
RETRY_MESSAGE = "No se pudo transcribir la nota de voz por ahora; se volverá a intentar."

logger = logging.getLogger("app.transcription")


def enqueue_transcription(db: DbSession, attachment_id: int):
    """Queue a ``transcribe_audio`` job for one attachment (caller commits)."""
    return jobs.enqueue(db, JOB_KIND, {"attachment_id": attachment_id}, message="En cola")


def apply_transcript(db: DbSession, sha256: str, text: str) -> int:
    """Copy ``text`` to every message whose audio file has ``sha256`` and no
    transcript yet; returns how many messages changed."""
    files = select(Attachment.id).where(Attachment.sha256 == sha256, Attachment.kind == AUDIO_KIND)
    result = db.execute(
        update(WhatsappMessage)
        .where(WhatsappMessage.attachment_id.in_(files), WhatsappMessage.transcript.is_(None))
        .values(transcript=text)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


@after_media
@after_import
def enqueue_transcriptions(db: DbSession, imp: WhatsappImport) -> None:
    if imp.mode not in TRANSCRIBE_MODES:
        return
    rows = db.execute(
        select(WhatsappMessage.id, Attachment.id, Attachment.sha256)
        .join(Attachment, Attachment.id == WhatsappMessage.attachment_id)
        .where(WhatsappMessage.media_import_id == imp.id, Attachment.kind == AUDIO_KIND)
        .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
    ).all()
    if not rows:
        return
    known = {
        t.sha256: t
        for t in db.scalars(
            select(AudioTranscript).where(AudioTranscript.sha256.in_({r[2] for r in rows}))
        )
    }
    queued: set[str] = set()
    reused = 0
    for _message_id, attachment_id, sha in rows:
        transcript = known.get(sha)
        if transcript is not None:
            if transcript.status == "done" and sha not in queued:
                reused += apply_transcript(db, sha, transcript.text or "")
                queued.add(sha)
            continue
        if sha in queued:
            continue
        queued.add(sha)
        enqueue_transcription(db, attachment_id)
    logger.info(
        "transcription: import %s has %s audio message(s), %s reused transcript(s)",
        imp.id,
        len(rows),
        reused,
    )


def _record_failure(db: DbSession, attachment: Attachment, code: str) -> None:
    db.add(
        AudioTranscript(
            sha256=attachment.sha256, attachment_id=attachment.id, status="failed", error=code
        )
    )
    db.commit()
    logger.warning("transcription: attachment %s failed for good: %s", attachment.id, code)
    raise jobs.PermanentJobError(FAILED_MESSAGES.get(code, FAILED_DEFAULT))


@jobs.handler(JOB_KIND)
def run_transcribe_audio(ctx: jobs.JobContext) -> None:
    attachment_id = ctx.payload.get("attachment_id")
    if not isinstance(attachment_id, int) or isinstance(attachment_id, bool):
        raise jobs.PermanentJobError("Tarea de transcripción inválida.")
    db = ctx.db
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise jobs.PermanentJobError("La nota de voz ya no existe.")
    sha = attachment.sha256

    # One provider call per file content, even with two jobs for the same hash.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"audio_transcript:{sha}"))))
    existing = db.scalar(select(AudioTranscript).where(AudioTranscript.sha256 == sha))
    if existing is not None:
        filled = apply_transcript(db, sha, existing.text or "") if existing.status == "done" else 0
        logger.info(
            "transcription: attachment %s already processed (%s); %s message(s) filled",
            attachment_id,
            existing.status,
            filled,
        )
        ctx.progress(100, "Esta nota de voz ya se había procesado")
        return

    settings = get_settings()
    try:
        provider = get_provider(settings)
    except TranscriptionNotConfigured:
        raise jobs.PermanentJobError(NOT_CONFIGURED) from None
    if attachment.size > settings.transcription_max_mb * 1024 * 1024:
        _record_failure(db, attachment, "too_large")
    fmt = upload_format(attachment.mime)
    if fmt is None:
        _record_failure(db, attachment, "unsupported")
    ext, mime = fmt
    try:
        storage = get_storage()
    except StorageNotConfigured:
        raise jobs.JobError("El almacenamiento de archivos no está disponible.") from None

    ctx.progress(20, "Transcribiendo la nota de voz")
    data = b"".join(storage.iter_object(attachment.storage_key))
    started = time.monotonic()
    try:
        result = provider.transcribe(
            data,
            filename=f"nota-de-voz.{ext}",
            mime=mime,
            language=LANGUAGE,
            prompt=hints.PROMPT,
            keywords=hints.keywords(db),
        )
    except TranscriptionError as exc:
        if exc.retryable:
            logger.warning(
                "transcription: attachment %s attempt %s failed: %s",
                attachment_id,
                ctx.attempt,
                exc.code,
            )
            raise jobs.JobError(RETRY_MESSAGE) from None
        if exc.config:
            logger.warning("transcription: provider refused the settings: %s", exc.code)
            raise jobs.PermanentJobError(
                "La transcripción automática no está configurada correctamente."
            ) from None
        _record_failure(db, attachment, exc.code)
    elapsed = time.monotonic() - started

    usage = dict(result.usage)
    if result.cost_usd is not None:
        usage["cost_usd"] = result.cost_usd
    db.add(
        AudioTranscript(
            sha256=sha,
            attachment_id=attachment.id,
            status="done",
            text=result.text,
            model=result.model,
            duration_s=(
                Decimal(str(round(result.duration_s, 2))) if result.duration_s is not None else None
            ),
            usage=usage,
        )
    )
    db.flush()
    filled = apply_transcript(db, sha, result.text)
    logger.info(
        "transcription: attachment %s done in %.1fs (%s bytes, audio %ss, est. USD %s, "
        "model %s); %s message(s) filled",
        attachment_id,
        elapsed,
        attachment.size,
        result.duration_s,
        result.cost_usd,
        result.model,
        filled,
    )
    ctx.progress(100, "Nota de voz transcrita")
