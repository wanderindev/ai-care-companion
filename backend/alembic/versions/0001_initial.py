"""Initial empty revision.

Revision ID: 0001
Revises:
Create Date: 2026-10-08

Establishes the migration chain; tables arrive with later tickets
(auth, patients, doctors, bitácora).
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
