"""Suggestions from code-run AI producers (ticket #125).

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-10

Widens the ``ck_suggestions_source`` CHECK constraint to also allow
``source = 'ai'``: the doctor inference (``app.services.doctor_inference``)
proposes a missing ``doctor_id`` for bitácora entries; it is neither a
WhatsApp message nor a chat. No row changes. The downgrade restores the
narrower constraint (it fails while ``'ai'`` rows exist: reject or delete
them first, an operator decision).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_suggestions_source", "suggestions", type_="check")
    op.create_check_constraint(
        "ck_suggestions_source", "suggestions", "source IN ('whatsapp', 'chat', 'ai')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_suggestions_source", "suggestions", type_="check")
    op.create_check_constraint(
        "ck_suggestions_source", "suggestions", "source IN ('whatsapp', 'chat')"
    )
