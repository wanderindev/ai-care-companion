"""Suggestions waiting for approval in «Pendientes».

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-09

Additive only (DESIGN §5.6, §6.7 step 5, ticket #38): a new table
``suggestions``. No existing table or row is touched (``log_entries.origin``
already allows ``whatsapp`` and ``chat``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suggestions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="whatsapp"),
        sa.Column(
            "patient_id",
            sa.BigInteger(),
            sa.ForeignKey("patients.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "source_message_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default=sa.text("'{}'::bigint[]"),
        ),
        sa.Column(
            "import_id",
            sa.BigInteger(),
            sa.ForeignKey("whatsapp_imports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "decided_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("result_ref", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('create_entry', 'update_entry', 'create_appointment', 'med_change', "
            "'lab_report', 'doctor')",
            name="ck_suggestions_kind",
        ),
        sa.CheckConstraint("source IN ('whatsapp', 'chat')", name="ck_suggestions_source"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'edited', 'rejected')",
            name="ck_suggestions_status",
        ),
    )
    op.create_index("ix_suggestions_status", "suggestions", ["status"])
    op.create_index("ix_suggestions_patient_id", "suggestions", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_suggestions_patient_id", table_name="suggestions")
    op.drop_index("ix_suggestions_status", table_name="suggestions")
    op.drop_table("suggestions")
