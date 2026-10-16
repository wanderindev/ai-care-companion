"""Chat: shared sessions and their messages.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-09

Additive only (DESIGN §5.7, ticket #31): two new tables, ``chat_sessions``
and ``chat_messages``. No existing table or row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _user_fk(column: str) -> sa.Column:
    return sa.Column(
        column, sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generating_since", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        _user_fk("created_by"),
        _user_fk("updated_by"),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        _user_fk("author_user_id"),
        sa.Column("status", sa.String(12), nullable=False, server_default=sa.text("'complete'")),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        sa.CheckConstraint("status IN ('complete', 'interrupted')", name="ck_chat_messages_status"),
    )
    op.create_index("ix_chat_messages_session_id_id", "chat_messages", ["session_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_id_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
