"""Analyte triage proposals (ticket #100).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-09

Additive: nullable ``analytes.triage`` (JSONB), Claude's proposal for an
analyte flagged ``needs_review`` (``app.services.labs.triage``): merge into
another analyte, keep as new with a cleaned name, or unsure, with confidence,
reason code and its review status. A JSON column rather than a table with a
FK to ``analytes.id``: ``merge_analytes`` re-points every such FK, which
would hand a merged analyte's proposal to its target. No row changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analytes", sa.Column("triage", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("analytes", "triage")
