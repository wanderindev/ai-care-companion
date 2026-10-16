"""Medication events: position of a derived event in its bitácora entry.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-09

Additive only (DESIGN §5.4, ticket #28): a nullable ``source_index`` on
``med_events`` (the event's position in its entry's
``origin_ref.medication_events``) and a unique constraint on
``(source_entry_id, source_index)``, so the backfill
(``python -m scripts.med_events backfill``) creates each derived event once.
Rows without a source (NULLs) never collide. No existing row is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("med_events", sa.Column("source_index", sa.SmallInteger(), nullable=True))
    op.create_unique_constraint(
        "uq_med_events_source", "med_events", ["source_entry_id", "source_index"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_med_events_source", "med_events", type_="unique")
    op.drop_column("med_events", "source_index")
