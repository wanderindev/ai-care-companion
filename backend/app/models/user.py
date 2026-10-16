"""Auth tables: allowlisted users, magic-link tokens and sessions (DESIGN §2, §5.1).

Ids are ``bigint``; ``users.id`` is what later tables reference as
``created_by`` / ``updated_by`` and ``audit_log.user_id``.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        CheckConstraint("role IN ('admin', 'member')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ROLE_MEMBER, server_default=ROLE_MEMBER
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # «Recibir invitaciones por correo» (DESIGN §6.3): email an .ics invite on
    # every appointment create/change/cancel (app.services.calendar_invites).
    email_invites: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # WhatsApp import area (ticket #96): imports, backfill analysis runs and
    # the sync extension's key. Narrower than admin: it counts only together
    # with the admin role (``is_import_admin``). Off for new users; migration
    # 0024 turned it on for the admins that existed then. Set and cleared with
    # ``python -m scripts.users import-access <email> on|off``.
    can_import: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_import_admin(self) -> bool:
        return self.is_admin and self.can_import


class MagicLinkToken(TimestampMixin, Base):
    """One row per link request; also the DB-backed rate-limit ledger.

    Requests for unknown or inactive emails are recorded too (``user_id``
    NULL, with a token that is never sent), so the per-email and per-IP limits
    behave the same whether or not the address is on the allowlist, and hold
    across uvicorn workers.
    """

    __tablename__ = "magic_link_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(45), index=True)


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(lazy="joined")
