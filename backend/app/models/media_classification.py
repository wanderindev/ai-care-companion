"""Media classifications (DESIGN §5.6, §6.7 step 3), deduped by file content.

``media_classifications`` — one row per image or PDF content (``sha256``,
unique) from a ``daily``/``backfill`` WhatsApp import that was classified, or
skipped for good: the same photo or PDF forwarded again, or exported twice, is
never sent to Claude twice. The result is copied to
``whatsapp_messages.classification`` of every message whose file has that
hash (``app.services.media_classification.jobs``).

``status``: ``done`` (``category`` set) or ``failed`` (``error`` is a short
code: ``too_large``, ``unsupported``, ``refusal``, ``truncated``,
``invalid_json``, ``api_error_400``…; never model text). ``method`` says how
a ``done`` row was decided: ``claude`` (one call), ``sticker`` (a WhatsApp
sticker, no call) or ``lab_extraction`` (the content was already read as a
lab report by the labs pipeline, no call). Transient and configuration
failures (network, 429/5xx, missing or invalid key) are not recorded, so the
job can run again.

PHI: ``description`` is a short, non-identifying Spanish description of a
medical document. Never log it.
"""

from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

CATEGORIES = ("lab_report", "prescription", "medical_document", "other")
PATIENT_HINTS = ("papa", "mama", "unknown")
CLASSIFICATION_STATUSES = ("done", "failed")
CLASSIFICATION_METHODS = ("claude", "sticker", "lab_extraction")
DESCRIPTION_MAX = 200


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class MediaClassification(TimestampMixin, Base):
    __tablename__ = "media_classifications"
    __table_args__ = (
        CheckConstraint(
            _in("status", CLASSIFICATION_STATUSES), name="ck_media_classifications_status"
        ),
        CheckConstraint(
            f"category IS NULL OR {_in('category', CATEGORIES)}",
            name="ck_media_classifications_category",
        ),
        CheckConstraint(
            f"patient_hint IS NULL OR {_in('patient_hint', PATIENT_HINTS)}",
            name="ck_media_classifications_patient_hint",
        ),
        CheckConstraint(
            f"method IS NULL OR {_in('method', CLASSIFICATION_METHODS)}",
            name="ck_media_classifications_method",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # The attachment that was classified (any row with this hash would do).
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachments.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    method: Mapped[str | None] = mapped_column(String(20))
    category: Mapped[str | None] = mapped_column(String(30))
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX))
    patient_hint: Mapped[str | None] = mapped_column(String(10))
    error: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
