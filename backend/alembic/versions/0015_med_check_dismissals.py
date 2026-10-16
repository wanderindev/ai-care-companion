"""Medication checks: «Ya está reflejado» dismissals of the out-of-date banner.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-09

Additive only (DESIGN §6.4, ticket #29): a new table ``med_check_dismissals``
(one row per bitácora entry the family marked as already reflected in the
medication list). No existing table or row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _user_fk(column: str) -> sa.Column:
    return sa.Column(
        column, sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


def upgrade() -> None:
    op.create_table(
        "med_check_dismissals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "patient_id",
            sa.BigInteger(),
            sa.ForeignKey("patients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "entry_id",
            sa.BigInteger(),
            sa.ForeignKey("log_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _user_fk("created_by"),
        _user_fk("updated_by"),
        sa.UniqueConstraint("entry_id", name="uq_med_check_dismissals_entry"),
    )
    op.create_index("ix_med_check_dismissals_patient_id", "med_check_dismissals", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_med_check_dismissals_patient_id", table_name="med_check_dismissals")
    op.drop_table("med_check_dismissals")
