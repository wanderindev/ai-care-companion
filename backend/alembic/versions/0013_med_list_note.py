"""Medication list: list-level note and effective date on each version.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-09

Additive only (DESIGN §5.4): two nullable columns on ``med_list_versions``.
``list_note`` holds list-level text that is not one medication (e.g. a
regimen table such as one drug's schedule by hour, in markdown);
``effective_date`` is the "Desde <fecha>" of the printed sheet. No existing
row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("med_list_versions", sa.Column("list_note", sa.Text(), nullable=True))
    op.add_column("med_list_versions", sa.Column("effective_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("med_list_versions", "effective_date")
    op.drop_column("med_list_versions", "list_note")
