"""Calendar feeds (DESIGN §5.3, §6.3): one secret ICS URL per user.

The URL is ``/api/v1/calendar/<token>.ics``. Only the token's SHA-256 is
stored (like magic links and sessions), so the full URL is shown once, right
after it is generated. Regenerating or turning the feed off sets
``revoked_at``; a user has at most one active (not revoked) feed, enforced by
a partial unique index.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin
from app.models.user import User


class CalendarFeed(TimestampMixin, Base):
    __tablename__ = "calendar_feeds"
    __table_args__ = (
        Index(
            "uq_calendar_feeds_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(lazy="joined")
