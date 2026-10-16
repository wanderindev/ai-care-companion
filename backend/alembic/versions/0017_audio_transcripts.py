"""Voice-note transcripts, deduped by file content.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-09

Additive only (DESIGN §5.6, §6.7, ticket #36): a new table
``audio_transcripts`` (one row per audio file SHA-256 sent to the
transcription provider, or skipped for good). No existing table or row is
touched; ``whatsapp_messages.transcript`` already exists (0006).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audio_transcripts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "attachment_id",
            sa.BigInteger(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("error", sa.String(50), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("duration_s", sa.Numeric(10, 2), nullable=True),
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
        sa.CheckConstraint("status IN ('done', 'failed')", name="ck_audio_transcripts_status"),
    )


def downgrade() -> None:
    op.drop_table("audio_transcripts")
