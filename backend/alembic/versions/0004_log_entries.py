"""Bitácora entries with Spanish, accent-insensitive full-text search.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08

Additive only: one function and one table.

``unaccent()`` is STABLE (it depends on the search_path to find its
dictionary), so it cannot appear in a generated column. ``immutable_unaccent``
wraps it with the dictionary named explicitly in the schema where the
extension lives (``public`` normally; looked up so a different schema still
works). The function is owned by the migrating role (``n16chat`` in prod);
the extension itself (0003) predates this app in production.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_TYPES = (
    "consulta_control",
    "consulta_enfermedad",
    "visita_domiciliaria",
    "teleconsulta",
    "terapia",
    "vacuna",
    "procedimiento",
    "hospitalizacion",
    "examen",
    "observacion_familiar",
    "otro",
)
CHANNELS = ("presencial", "domicilio", "whatsapp", "telefono", "correo")
REVIEW_STATUSES = ("ok", "pending_review")
ORIGINS = ("import_docx", "whatsapp", "manual", "chat")

SEARCH_VECTOR_SQL = (
    "to_tsvector('spanish'::regconfig, public.immutable_unaccent("
    "coalesce(title, '') || ' ' || coalesce(source_label, '') || ' ' || "
    "coalesce(indications, '') || ' ' || coalesce(family_notes, '')))"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    schema = op.get_bind().scalar(
        sa.text(
            "SELECT n.nspname FROM pg_extension e"
            " JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'unaccent'"
        )
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.immutable_unaccent(text) RETURNS text"
        " LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT"
        f" AS $$ SELECT {schema}.unaccent('{schema}.unaccent'::regdictionary, $1) $$"
    )

    op.create_table(
        "log_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "patient_id",
            sa.BigInteger(),
            sa.ForeignKey("patients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_time", sa.Time(), nullable=True),
        sa.Column(
            "doctor_id",
            sa.BigInteger(),
            sa.ForeignKey("doctors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_label", sa.String(200), nullable=True),
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("channel", sa.String(20), nullable=True),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("indications", sa.Text(), nullable=True),
        sa.Column("family_notes", sa.Text(), nullable=True),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="ok"),
        sa.Column("origin", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("origin_ref", postgresql.JSONB(), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
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
        sa.CheckConstraint(_in("entry_type", ENTRY_TYPES), name="ck_log_entries_entry_type"),
        sa.CheckConstraint(
            "channel IS NULL OR " + _in("channel", CHANNELS), name="ck_log_entries_channel"
        ),
        sa.CheckConstraint(
            _in("review_status", REVIEW_STATUSES), name="ck_log_entries_review_status"
        ),
        sa.CheckConstraint(_in("origin", ORIGINS), name="ck_log_entries_origin"),
    )
    op.create_index("ix_log_entries_patient_date", "log_entries", ["patient_id", "entry_date"])
    op.create_index("ix_log_entries_doctor_id", "log_entries", ["doctor_id"])
    op.create_index(
        "ix_log_entries_search_vector",
        "log_entries",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_table("log_entries")
    op.execute("DROP FUNCTION IF EXISTS public.immutable_unaccent(text)")
