"""WhatsApp ingestion (DESIGN §5.6): uploaded exports and their messages.

``whatsapp_imports`` — one row per uploaded export zip. The zip arrives in
chunks straight into an S3 multipart upload (``upload_id`` / ``staging_key``,
see ``app.services.whatsapp_import.upload``), then the worker job
(``job_id``) parses it, stores new messages and their media, and records
``range_from``/``range_to`` and ``stats``. Status: ``uploading`` → ``queued``
→ ``processing`` → ``done``; ``failed`` when the zip is not a usable export;
``cancelled`` when the upload was abandoned. ``attachment_id`` is the stored
zip (an ``attachments`` row owned by this import; deduped by SHA-256).

``whatsapp_messages`` — every non-system message ever imported, once:
``dedupe_hash`` (unique) is ``app.services.whatsapp.dedupe_hash`` (sent_at +
sender + text + a has-media flag), so overlapping exports, and an export
without media followed by one with media, never duplicate a message.
``import_id`` is the import that first stored the message, ``media_import_id``
the one that supplied its file (``attachment_id``, an attachment owned by the
message). Lab extraction finds new PDFs with
``app.services.whatsapp_import.pdf_messages``. ``transcript`` and
``classification`` are filled by Epic 6. The sync extension adds files to
stored messages afterwards (``app.services.whatsapp_import.media_upload``;
``media_import_id`` = the import of the extension run that uploaded it,
``wa_key`` = the WhatsApp Web message key it matched, unique when set).

PHI: ``sender``, ``text``, ``media_filename`` and ``filename`` are family
messages and names. Never log them.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.job import Job
from app.models.mixins import TimestampMixin
from app.models.user import User

IMPORT_MODES = ("labs_only", "backfill", "daily")
IMPORT_STATUSES = ("uploading", "queued", "processing", "done", "failed", "cancelled")
MESSAGE_MEDIA_KINDS = ("audio", "image", "pdf", "video", "other")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class WhatsappImport(TimestampMixin, Base):
    __tablename__ = "whatsapp_imports"
    __table_args__ = (
        CheckConstraint(_in("mode", IMPORT_MODES), name="ck_whatsapp_imports_mode"),
        CheckConstraint(_in("status", IMPORT_STATUSES), name="ck_whatsapp_imports_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uploaded_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    patient_focus_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="uploading")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # While uploading: the S3 multipart upload. The staging object becomes the
    # stored zip (or is deleted when an identical zip is already stored).
    staging_key: Mapped[str | None] = mapped_column(String(255))
    upload_id: Mapped[str | None] = mapped_column(String(1024))
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachments.id", ondelete="SET NULL")
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL")
    )
    range_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    range_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Sync extension runs (docs/extension-api.md): WhatsApp Web exports text
    # only, so the extension uploads files for stored messages afterwards
    # (``media_upload``). While ``awaiting_media`` the daily analysis waits
    # (bounded by ``whatsapp_import.process.MEDIA_WAIT``). ``media_completed_at``
    # is set when the extension says it is done; the ``whatsapp_media`` job
    # then runs the ``after_media`` hooks and clears ``awaiting_media``.
    awaiting_media: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    media_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    uploader: Mapped[User | None] = relationship(lazy="selectin", viewonly=True)
    job: Mapped[Job | None] = relationship(lazy="selectin", viewonly=True)


class WhatsappMessage(TimestampMixin, Base):
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        CheckConstraint(
            "media_kind IS NULL OR " + _in("media_kind", MESSAGE_MEDIA_KINDS),
            name="ck_whatsapp_messages_media_kind",
        ),
        Index("ix_whatsapp_messages_sent_at", "sent_at"),
        Index("ix_whatsapp_messages_import_id", "import_id"),
        Index("ix_whatsapp_messages_media_import_id", "media_import_id"),
        Index(
            "uq_whatsapp_messages_wa_key",
            "wa_key",
            unique=True,
            postgresql_where=text("wa_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    import_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("whatsapp_imports.id", ondelete="RESTRICT"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sender: Mapped[str | None] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    forwarded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    media_kind: Mapped[str | None] = mapped_column(String(10))
    media_filename: Mapped[str | None] = mapped_column(String(255))
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachments.id", ondelete="SET NULL")
    )
    media_import_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("whatsapp_imports.id", ondelete="RESTRICT")
    )
    transcript: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dedupe_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # WhatsApp Web's message key (the bubble's 32-hex ``data-id``), recorded
    # when the sync extension uploads the file it matched to this message.
    wa_key: Mapped[str | None] = mapped_column(String(32))
