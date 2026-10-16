"""Image and PDF classifications, deduped by file content.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-09

Additive only (DESIGN §5.6, §6.7, ticket #37): a new table
``media_classifications`` (one row per image/PDF SHA-256 classified by Claude,
or decided without a call). No existing table or row is touched;
``whatsapp_messages.classification`` already exists (0006).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_classifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "attachment_id",
            sa.BigInteger(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("method", sa.String(20), nullable=True),
        sa.Column("category", sa.String(30), nullable=True),
        sa.Column("description", sa.String(200), nullable=True),
        sa.Column("patient_hint", sa.String(10), nullable=True),
        sa.Column("error", sa.String(50), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column(
            "usage",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('done', 'failed')", name="ck_media_classifications_status"),
        sa.CheckConstraint(
            "category IS NULL OR category IN "
            "('lab_report', 'prescription', 'medical_document', 'other')",
            name="ck_media_classifications_category",
        ),
        sa.CheckConstraint(
            "patient_hint IS NULL OR patient_hint IN ('papa', 'mama', 'unknown')",
            name="ck_media_classifications_patient_hint",
        ),
        sa.CheckConstraint(
            "method IS NULL OR method IN ('claude', 'sticker', 'lab_extraction')",
            name="ck_media_classifications_method",
        ),
    )


def downgrade() -> None:
    op.drop_table("media_classifications")
