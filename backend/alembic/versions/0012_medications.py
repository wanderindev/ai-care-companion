"""Medications (med_items, med_avoid, med_list_versions, med_events).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-09

Additive only (DESIGN §5.4): four new tables. No existing row is touched; the
lists are seeded later by the operator-run importer (ticket #24).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLOTS = (
    "al_despertar",
    "desayuno",
    "manana",
    "almuerzo",
    "tarde",
    "cena",
    "noche",
    "madrugada",
    "segun_necesidad",
    "otros",
)
EVENT_KINDS = ("inicio", "suspension", "cambio_dosis", "sustitucion")
REVIEW_STATUSES = ("ok", "pending_review")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(column: str, target: str, ondelete: str = "SET NULL") -> sa.Column:
    return sa.Column(
        column, sa.BigInteger(), sa.ForeignKey(target, ondelete=ondelete), nullable=True
    )


def _patient() -> sa.Column:
    return sa.Column(
        "patient_id",
        sa.BigInteger(),
        sa.ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _authors() -> list[sa.Column]:
    return [_fk("created_by", "users.id"), _fk("updated_by", "users.id")]


def upgrade() -> None:
    op.create_table(
        "med_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _patient(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("strength", sa.String(100), nullable=True),
        sa.Column("quantity", sa.String(50), nullable=True),
        sa.Column("dose_note", sa.String(200), nullable=True),
        sa.Column("slot", sa.String(20), nullable=False),
        sa.Column("time", sa.Time(), nullable=True),
        sa.Column("days", postgresql.ARRAY(sa.SmallInteger()), nullable=True),
        sa.Column("schedule_note", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        _fk("prescriber_id", "doctors.id"),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _fk("source_entry_id", "log_entries.id"),
        *_timestamps(),
        *_authors(),
        sa.CheckConstraint(_in("slot", SLOTS), name="ck_med_items_slot"),
        sa.CheckConstraint(
            "days IS NULL OR (cardinality(days) > 0 AND days <@ ARRAY[1,2,3,4,5,6,7]::smallint[])",
            name="ck_med_items_days",
        ),
    )
    op.create_index("ix_med_items_patient_slot", "med_items", ["patient_id", "slot", "sort_order"])
    op.create_index("ix_med_items_prescriber_id", "med_items", ["prescriber_id"])
    op.create_index("ix_med_items_source_entry_id", "med_items", ["source_entry_id"])

    op.create_table(
        "med_avoid",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _patient(),
        sa.Column("substance", sa.String(200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _fk("source_entry_id", "log_entries.id"),
        *_timestamps(),
        *_authors(),
    )
    op.create_index("ix_med_avoid_patient_id", "med_avoid", ["patient_id"])
    op.create_index("ix_med_avoid_source_entry_id", "med_avoid", ["source_entry_id"])

    op.create_table(
        "med_list_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _patient(),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        _fk("changed_by", "users.id"),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        *_timestamps(),
        sa.UniqueConstraint("patient_id", "version_no", name="uq_med_list_versions_patient_no"),
        sa.CheckConstraint("version_no >= 1", name="ck_med_list_versions_version_no"),
    )

    op.create_table(
        "med_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _patient(),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("medication", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        _fk("source_entry_id", "log_entries.id"),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="ok"),
        *_timestamps(),
        *_authors(),
        sa.CheckConstraint(_in("kind", EVENT_KINDS), name="ck_med_events_kind"),
        sa.CheckConstraint(
            _in("review_status", REVIEW_STATUSES), name="ck_med_events_review_status"
        ),
    )
    op.create_index("ix_med_events_patient_date", "med_events", ["patient_id", "event_date"])
    op.create_index("ix_med_events_source_entry_id", "med_events", ["source_entry_id"])


def downgrade() -> None:
    op.drop_index("ix_med_events_source_entry_id", table_name="med_events")
    op.drop_index("ix_med_events_patient_date", table_name="med_events")
    op.drop_table("med_events")
    op.drop_table("med_list_versions")
    op.drop_index("ix_med_avoid_source_entry_id", table_name="med_avoid")
    op.drop_index("ix_med_avoid_patient_id", table_name="med_avoid")
    op.drop_table("med_avoid")
    op.drop_index("ix_med_items_source_entry_id", table_name="med_items")
    op.drop_index("ix_med_items_prescriber_id", table_name="med_items")
    op.drop_index("ix_med_items_patient_slot", table_name="med_items")
    op.drop_table("med_items")
