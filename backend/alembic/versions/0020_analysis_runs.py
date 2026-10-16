"""Backfill analysis runs.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-09

Additive only (DESIGN §6.7 step 6, ticket #40): a new table
``analysis_runs`` and a nullable ``suggestions.analysis_run_id`` (+ index).
No existing row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default="backfill"),
        sa.Column(
            "patient_id",
            sa.BigInteger(),
            sa.ForeignKey("patients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("cross_check", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reanalyze", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("stop_reason", sa.String(20), nullable=True),
        sa.Column("max_cost_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "stats", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("kind IN ('backfill')", name="ck_analysis_runs_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'waiting', 'running', 'done', 'stopped')",
            name="ck_analysis_runs_status",
        ),
        sa.CheckConstraint("date_from <= date_to", name="ck_analysis_runs_range"),
    )
    op.add_column(
        "suggestions",
        sa.Column(
            "analysis_run_id",
            sa.BigInteger(),
            sa.ForeignKey("analysis_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_suggestions_analysis_run_id", "suggestions", ["analysis_run_id"])


def downgrade() -> None:
    op.drop_index("ix_suggestions_analysis_run_id", table_name="suggestions")
    op.drop_column("suggestions", "analysis_run_id")
    op.drop_table("analysis_runs")
