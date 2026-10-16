"""Lab portal links shared in the chat (lab_links).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08

Additive only (DESIGN §5.5): one new table. No existing row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINK_STATUSES = ("pending", "fetched", "failed", "ignored")


def _fk(column: str, target: str) -> sa.Column:
    return sa.Column(
        column, sa.BigInteger(), sa.ForeignKey(target, ondelete="SET NULL"), nullable=True
    )


def upgrade() -> None:
    op.create_table(
        "lab_links",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("host", sa.String(255), nullable=False),
        _fk("message_id", "whatsapp_messages.id"),
        _fk("patient_id", "patients.id"),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(30), nullable=True),
        _fk("attachment_id", "attachments.id"),
        _fk("report_id", "lab_reports.id"),
        _fk("job_id", "jobs.id"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _fk("created_by", "users.id"),
        _fk("updated_by", "users.id"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in LINK_STATUSES) + ")",
            name="ck_lab_links_status",
        ),
    )
    op.create_index("ix_lab_links_status", "lab_links", ["status"])
    op.create_index("ix_lab_links_message_id", "lab_links", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_lab_links_message_id", table_name="lab_links")
    op.drop_index("ix_lab_links_status", table_name="lab_links")
    op.drop_table("lab_links")
