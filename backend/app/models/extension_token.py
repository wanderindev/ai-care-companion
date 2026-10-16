"""API tokens of the WhatsApp Web sync extension (DESIGN §6.8, docs/extension-api.md).

The extension authenticates with ``Authorization: Bearer <token>``: an admin
generates the token in the app («Importar WhatsApp» → «Extensión de Chrome»)
and pastes it into the extension once. Like the calendar feed, only the
token's SHA-256 is stored (the token is shown once), a user has at most one
active token (partial unique index), and regenerating or revoking sets
``revoked_at``. The token only opens the WhatsApp import endpoints, and only
while its user is an active admin (``app.api.deps.require_import_admin``).
``last_used_at`` is refreshed at most once per hour.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin
from app.models.user import User


class ExtensionToken(TimestampMixin, Base):
    __tablename__ = "extension_tokens"
    __table_args__ = (
        Index(
            "uq_extension_tokens_active_user",
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
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(lazy="joined")
