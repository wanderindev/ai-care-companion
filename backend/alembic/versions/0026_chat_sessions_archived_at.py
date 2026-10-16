"""Archived chat sessions (ticket #99).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-09

Additive: nullable ``chat_sessions.archived_at``. An archived conversation is
hidden from the default list and shown under «Archivadas» until restored. No
row changes. ``suggestions.chat_session_id`` is already ``ON DELETE SET NULL``
(migration 0021) and ``chat_messages.session_id`` ``ON DELETE CASCADE``
(0016), so deleting a session needs no FK change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "archived_at")
