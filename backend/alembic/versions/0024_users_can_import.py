"""Import permission on users (ticket #96).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-09

Additive: ``users.can_import`` (boolean, default false). The WhatsApp import
area (imports, backfill analysis runs, the sync extension's key) needs an
admin **with** this flag. To keep today's behaviour, every user who is an
admin when the migration runs gets ``true`` (a data write, but one that only
preserves what those users can already do); admins added later start without
it. Nothing else changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("can_import", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE users SET can_import = true WHERE role = 'admin'")


def downgrade() -> None:
    op.drop_column("users", "can_import")
