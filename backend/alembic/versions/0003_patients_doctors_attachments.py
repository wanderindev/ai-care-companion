"""Patients, doctors, audit log and attachments; seed Papá / Mamá.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08

Additive only (four new tables, two seeded rows). Also ``CREATE EXTENSION IF
NOT EXISTS unaccent`` for the full-text search of later tickets (already
installed in production, so a no-op there; trusted since PG13).

The seed holds display names only. Full name, ID number (CIP), birth date,
diagnoses and notes are PHI and are filled in by the operator in the app or
with ``python -m scripts.patients set`` — never in git.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_TYPES = ("log_entry", "lab_report", "chat_message", "appointment", "whatsapp_message")


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
    return [
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    patients = op.create_table(
        "patients",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("slug", sa.String(20), nullable=False, unique=True),
        sa.Column("display_name", sa.String(50), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("id_number", sa.String(50), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("diagnoses", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        *_authors(),
    )
    op.bulk_insert(
        patients,
        [
            {"slug": "papa", "display_name": "Papá"},
            {"slug": "mama", "display_name": "Mamá"},
        ],
    )

    op.create_table(
        "doctors",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("specialty", sa.String(100), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("clinic", sa.String(200), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
        *_authors(),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("entity", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_entity", "audit_log", ["entity", "entity_id"])

    owner_types = ", ".join(f"'{t}'" for t in OWNER_TYPES)
    op.create_table(
        "attachments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_type", sa.String(30), nullable=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "uploaded_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            f"owner_type IS NULL OR owner_type IN ({owner_types})",
            name="ck_attachments_owner_type",
        ),
        sa.CheckConstraint(
            "(owner_type IS NULL) = (owner_id IS NULL)", name="ck_attachments_owner_pair"
        ),
    )
    op.create_index("ix_attachments_owner", "attachments", ["owner_type", "owner_id"])
    op.create_index("ix_attachments_sha256", "attachments", ["sha256"])


def downgrade() -> None:
    op.drop_table("attachments")
    op.drop_table("audit_log")
    op.drop_table("doctors")
    op.drop_table("patients")
    # The unaccent extension is left in place (it predates this app in prod).
