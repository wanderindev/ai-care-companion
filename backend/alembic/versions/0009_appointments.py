"""Appointments (appointments).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08

Additive only (DESIGN §5.3): one new table. No existing row is touched; the
backfill from imported bitácora entries is a separate operator step
(``python -m scripts.appointments backfill``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("programada", "realizada", "cancelada")


def _fk(column: str, target: str, ondelete: str = "SET NULL", **kw) -> sa.Column:
    return sa.Column(
        column, sa.BigInteger(), sa.ForeignKey(target, ondelete=ondelete), nullable=True, **kw
    )


def upgrade() -> None:
    op.create_table(
        "appointments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "patient_id",
            sa.BigInteger(),
            sa.ForeignKey("patients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _fk("doctor_id", "doctors.id"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("location", sa.String(300), nullable=True),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="programada"),
        _fk("created_from_entry_id", "log_entries.id", unique=True),
        _fk("result_entry_id", "log_entries.id"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _fk("created_by", "users.id"),
        _fk("updated_by", "users.id"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")",
            name="ck_appointments_status",
        ),
        sa.CheckConstraint("duration_min BETWEEN 1 AND 1440", name="ck_appointments_duration"),
    )
    op.create_index(
        "ix_appointments_patient_starts_at", "appointments", ["patient_id", "starts_at"]
    )
    op.create_index("ix_appointments_status", "appointments", ["status"])
    op.create_index("ix_appointments_doctor_id", "appointments", ["doctor_id"])
    op.create_index("ix_appointments_result_entry_id", "appointments", ["result_entry_id"])


def downgrade() -> None:
    op.drop_index("ix_appointments_result_entry_id", table_name="appointments")
    op.drop_index("ix_appointments_doctor_id", table_name="appointments")
    op.drop_index("ix_appointments_status", table_name="appointments")
    op.drop_index("ix_appointments_patient_starts_at", table_name="appointments")
    op.drop_table("appointments")
