"""LEDD: classified medication names outside the reference table (ticket #133).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-11

Additive only: a new table ``ledd_substances`` (one row per normalized
medication name that the built-in LEDD catalogue does not know, with
Claude's proposal and the admin's decision; ``app.models.ledd``). No existing
table or row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ledd_substances",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name_key", sa.String(200), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("strength", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("is_dopaminergic", sa.Boolean(), nullable=True),
        sa.Column("mechanism", sa.String(30), nullable=True),
        sa.Column("factor", sa.Float(), nullable=True),
        sa.Column("generic_name", sa.String(200), nullable=True),
        sa.Column("source", sa.String(300), nullable=True),
        sa.Column("confidence", sa.String(10), nullable=True),
        sa.Column("error", sa.String(50), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column(
            "usage", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "reviewed_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'proposed', 'confirmed', 'rejected', 'failed')",
            name="ck_ledd_substances_status",
        ),
        sa.CheckConstraint(
            "mechanism IS NULL OR mechanism IN ('levodopa', 'levodopa_cr', 'comt_inhibitor', "
            "'dopamine_agonist', 'mao_b_inhibitor', 'nmda_antagonist', 'other_dopaminergic', "
            "'not_dopaminergic', 'unknown')",
            name="ck_ledd_substances_mechanism",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence IN ('high', 'medium', 'low')",
            name="ck_ledd_substances_confidence",
        ),
        sa.CheckConstraint(
            "factor IS NULL OR (factor >= 0 AND factor <= 1000)",
            name="ck_ledd_substances_factor",
        ),
    )


def downgrade() -> None:
    op.drop_table("ledd_substances")
