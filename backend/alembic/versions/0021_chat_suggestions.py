"""Suggestions proposed from the chat.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-09

Additive only (ticket #41): nullable ``suggestions.chat_session_id`` (the
conversation a ``source='chat'`` suggestion was proposed in, + index) and
``suggestions.created_by`` (the family member whose message led to it). No
existing row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "suggestions",
        sa.Column(
            "chat_session_id",
            sa.BigInteger(),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "suggestions",
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_suggestions_chat_session_id", "suggestions", ["chat_session_id"])


def downgrade() -> None:
    op.drop_index("ix_suggestions_chat_session_id", table_name="suggestions")
    op.drop_column("suggestions", "created_by")
    op.drop_column("suggestions", "chat_session_id")
