"""Per-user email calendar invites (users.email_invites).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-09

Additive only (DESIGN §6.3): one boolean column, ``false`` for every existing
user (opt-in). No existing value is changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_invites", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("users", "email_invites")
