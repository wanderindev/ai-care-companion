"""Attachments (DESIGN §5.2): files in DO Spaces with a polymorphic owner.

Upload first, link later: ``POST /attachments`` creates an *unlinked* row
(``owner_type``/``owner_id`` NULL) and returns its id; the form that owns the
file (a log entry in #5/#6, a lab report, …) sends the ids when it is saved and
the server links them with ``app.services.attachments.link_attachments``.
Both owner columns are NULL or both set (check constraint). There is no FK on
``owner_id`` because the owner table depends on ``owner_type``.

Storage is content-addressed (``<kind>/<yyyy>/<sha256>.<ext>``) and deduped by
SHA-256 (one exception: an uploaded WhatsApp export zip keeps the key of its
chunked upload, ``archives/<yyyy>/upload-<import id>-<random>.zip``, since
its hash is only known once every chunk has arrived; it still dedupes by
SHA-256): uploading the same bytes twice creates a second row (it may belong to
another entry, with another filename) pointing at the same object. Never delete
an object while another row still references its ``storage_key``.
"""

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

OWNER_TYPES = (
    "log_entry",
    "lab_report",
    "chat_message",
    "appointment",
    "whatsapp_message",
    "whatsapp_import",
)


class Attachment(TimestampMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "owner_type IS NULL OR owner_type IN ("
            + ", ".join(f"'{t}'" for t in OWNER_TYPES)
            + ")",
            name="ck_attachments_owner_type",
        ),
        CheckConstraint(
            "(owner_type IS NULL) = (owner_id IS NULL)", name="ck_attachments_owner_pair"
        ),
        Index("ix_attachments_owner", "owner_type", "owner_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_type: Mapped[str | None] = mapped_column(String(30))
    owner_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uploaded_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
