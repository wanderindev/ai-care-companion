"""Calendar feeds (calendar_feeds).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08

Additive only (DESIGN §5.3): one new table holding the SHA-256 of each user's
secret ICS feed token. No existing row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_feeds",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_calendar_feeds_user_id", "calendar_feeds", ["user_id"])
    op.create_index(
        "uq_calendar_feeds_active_user",
        "calendar_feeds",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_calendar_feeds_active_user", table_name="calendar_feeds")
    op.drop_index("ix_calendar_feeds_user_id", table_name="calendar_feeds")
    op.drop_table("calendar_feeds")
