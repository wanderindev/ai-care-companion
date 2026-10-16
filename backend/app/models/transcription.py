"""Voice-note transcripts (DESIGN §5.6, §6.7 step 3), deduped by file content.

``audio_transcripts`` — one row per audio file content (``sha256``, unique)
ever sent to the transcription provider, or skipped for good: the same voice
note forwarded again, or exported twice, is never sent twice. The text is
copied to ``whatsapp_messages.transcript`` of every message whose file has
that hash (``app.services.transcription.jobs``).

``status``: ``done`` (``text`` set, possibly empty for a silent note) or
``failed`` (``error`` is a short code: ``too_large``, ``unsupported``,
``rejected``; never provider text). Transient and configuration failures
(network, 429/5xx, missing or invalid key) are not recorded, so the job can
run again.

PHI: ``text`` is family speech. Never log it.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

TRANSCRIPT_STATUSES = ("done", "failed")


class AudioTranscript(TimestampMixin, Base):
    __tablename__ = "audio_transcripts"
    __table_args__ = (
        CheckConstraint("status IN ('done', 'failed')", name="ck_audio_transcripts_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # The attachment that was sent (any row with this hash would do).
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachments.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    duration_s: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
