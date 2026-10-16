"""Lab report patient match methods (ticket #98).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-09

Additive: ``lab_reports.patient_match`` widens from ``varchar(10)`` to
``varchar(20)`` and its check constraint accepts two more methods,
``name_variant`` (a tolerant name match: another word order, a missing second
surname or married "de …" part) and ``llm`` (Claude's high-confidence answer
when the deterministic rules give no single patient). No row changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("cip", "name", "manual")
NEW = ("cip", "name", "name_variant", "llm", "manual")
CONSTRAINT = "ck_lab_reports_patient_match"


def _check(values: tuple[str, ...]) -> str:
    return "patient_match IS NULL OR patient_match IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "lab_reports", type_="check")
    op.alter_column(
        "lab_reports",
        "patient_match",
        type_=sa.String(20),
        existing_type=sa.String(10),
        existing_nullable=True,
    )
    op.create_check_constraint(CONSTRAINT, "lab_reports", _check(NEW))


def downgrade() -> None:
    op.execute(
        "UPDATE lab_reports SET patient_match = NULL WHERE patient_match IN ('name_variant', 'llm')"
    )
    op.drop_constraint(CONSTRAINT, "lab_reports", type_="check")
    op.alter_column(
        "lab_reports",
        "patient_match",
        type_=sa.String(10),
        existing_type=sa.String(20),
        existing_nullable=True,
    )
    op.create_check_constraint(CONSTRAINT, "lab_reports", _check(OLD))
