"""WhatsApp Web sync extension: API tokens and media uploads.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-09

Additive only (ticket #43): a new ``extension_tokens`` table (SHA-256 of
each admin's extension token, one active per user) and two columns on
``whatsapp_imports`` (``awaiting_media`` default false, nullable
``media_completed_at``). No existing row is changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extension_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_extension_tokens_user_id", "extension_tokens", ["user_id"])
    op.create_index(
        "uq_extension_tokens_active_user",
        "extension_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.add_column(
        "whatsapp_imports",
        sa.Column("awaiting_media", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "whatsapp_imports",
        sa.Column("media_completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_imports", "media_completed_at")
    op.drop_column("whatsapp_imports", "awaiting_media")
    op.drop_index("uq_extension_tokens_active_user", table_name="extension_tokens")
    op.drop_index("ix_extension_tokens_user_id", table_name="extension_tokens")
    op.drop_table("extension_tokens")
