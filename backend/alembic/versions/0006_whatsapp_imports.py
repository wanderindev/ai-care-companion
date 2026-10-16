"""WhatsApp imports and messages.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08

Additive only (DESIGN §5.6): two new tables, and the attachments owner-type
check constraint is widened to allow ``whatsapp_import`` (the stored export
zip). No existing row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_OWNER_TYPES = ("log_entry", "lab_report", "chat_message", "appointment", "whatsapp_message")
OWNER_TYPES = (*OLD_OWNER_TYPES, "whatsapp_import")
IMPORT_MODES = ("labs_only", "backfill", "daily")
IMPORT_STATUSES = ("uploading", "queued", "processing", "done", "failed", "cancelled")
MEDIA_KINDS = ("audio", "image", "pdf", "video", "other")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _owner_check(types: tuple[str, ...]) -> None:
    op.drop_constraint("ck_attachments_owner_type", "attachments", type_="check")
    op.create_check_constraint(
        "ck_attachments_owner_type",
        "attachments",
        "owner_type IS NULL OR " + _in("owner_type", types),
    )


def upgrade() -> None:
    _owner_check(OWNER_TYPES)

    op.create_table(
        "whatsapp_imports",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "uploaded_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column(
            "patient_focus_id",
            sa.BigInteger(),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploading"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("staging_key", sa.String(255), nullable=True),
        sa.Column("upload_id", sa.String(1024), nullable=True),
        sa.Column(
            "attachment_id",
            sa.BigInteger(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("range_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("range_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "stats", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_in("mode", IMPORT_MODES), name="ck_whatsapp_imports_mode"),
        sa.CheckConstraint(_in("status", IMPORT_STATUSES), name="ck_whatsapp_imports_status"),
    )

    op.create_table(
        "whatsapp_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "import_id",
            sa.BigInteger(),
            sa.ForeignKey("whatsapp_imports.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender", sa.String(200), nullable=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("forwarded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("media_kind", sa.String(10), nullable=True),
        sa.Column("media_filename", sa.String(255), nullable=True),
        sa.Column(
            "attachment_id",
            sa.BigInteger(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "media_import_id",
            sa.BigInteger(),
            sa.ForeignKey("whatsapp_imports.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("classification", postgresql.JSONB(), nullable=True),
        sa.Column("dedupe_hash", sa.String(64), nullable=False, unique=True),
        *_timestamps(),
        sa.CheckConstraint(
            "media_kind IS NULL OR " + _in("media_kind", MEDIA_KINDS),
            name="ck_whatsapp_messages_media_kind",
        ),
    )
    op.create_index("ix_whatsapp_messages_sent_at", "whatsapp_messages", ["sent_at"])
    op.create_index("ix_whatsapp_messages_import_id", "whatsapp_messages", ["import_id"])
    op.create_index(
        "ix_whatsapp_messages_media_import_id", "whatsapp_messages", ["media_import_id"]
    )


def downgrade() -> None:
    op.drop_table("whatsapp_messages")
    op.drop_table("whatsapp_imports")
    # Stored zips become unlinked attachments (the rows and objects stay).
    op.execute(
        "UPDATE attachments SET owner_type = NULL, owner_id = NULL"
        " WHERE owner_type = 'whatsapp_import'"
    )
    _owner_check(OLD_OWNER_TYPES)
