"""WhatsApp Web message key on stored messages.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-09

Additive only (ticket #45): a nullable ``whatsapp_messages.wa_key`` (the
32-hex ``data-id`` of a WhatsApp Web bubble, recorded when the sync
extension uploads that message's file) with a unique index where it is set.
No existing row is changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("whatsapp_messages", sa.Column("wa_key", sa.String(32), nullable=True))
    op.create_index(
        "uq_whatsapp_messages_wa_key",
        "whatsapp_messages",
        ["wa_key"],
        unique=True,
        postgresql_where=sa.text("wa_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_whatsapp_messages_wa_key", table_name="whatsapp_messages")
    op.drop_column("whatsapp_messages", "wa_key")
