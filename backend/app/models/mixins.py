from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class TimestampMixin:
    """``created_at`` / ``updated_at`` (timestamptz) on every table (DESIGN §5)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AuthorMixin:
    """``created_by`` / ``updated_by`` on user-edited tables (DESIGN §5).

    Nullable with ``ON DELETE SET NULL``: rows seeded by migrations or written
    by CLI scripts have no acting user, and removing a user keeps their edits.
    """

    @declared_attr
    def created_by(cls) -> Mapped[int | None]:
        return mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))

    @declared_attr
    def updated_by(cls) -> Mapped[int | None]:
        return mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
