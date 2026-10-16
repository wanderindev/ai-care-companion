"""Migrations apply cleanly to an empty PostgreSQL 17 database."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from testcontainers.community.postgres import PostgresContainer

import app.models  # noqa: F401  -- registers every model
from app.core.database import Base

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["configure_logger"] = False
    return cfg


def test_single_head():
    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"


def test_upgrade_head_on_empty_database(monkeypatch):
    # A dedicated container: the shared session one is built with create_all,
    # which would hide a migration that fails on a truly empty database.
    with PostgresContainer("postgres:17-alpine") as postgres:
        url = postgres.get_connection_url()
        monkeypatch.setenv("DATABASE_URL", url)
        cfg = _alembic_config()

        command.upgrade(cfg, "head")

        head = ScriptDirectory.from_config(cfg).get_current_head()
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
                # 0003 seeds the two patients with display names only (no PHI).
                seeded = conn.execute(
                    text(
                        "SELECT slug, display_name, full_name, id_number, birth_date"
                        " FROM patients ORDER BY id"
                    )
                ).all()
                unaccent = conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'unaccent'")
                ).scalar()
                # 0004: the IMMUTABLE wrapper the generated search_vector uses.
                unaccented = conn.execute(
                    text(
                        "SELECT public.immutable_unaccent('Neumonía'), provolatile"
                        " FROM pg_proc WHERE proname = 'immutable_unaccent'"
                    )
                ).one()
                # 0007 seeds the common analytes (reference data, no PHI).
                analytes = conn.execute(
                    text("SELECT count(*), count(*) FILTER (WHERE needs_review) FROM analytes")
                ).one()
                b12 = conn.execute(
                    text("SELECT specimen FROM analytes WHERE 'Cobalamina' = ANY(aliases)")
                ).scalar()
                # The migrated schema matches the models (no autogenerate drift).
                diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        finally:
            engine.dispose()
        assert current == head
        assert [tuple(r) for r in seeded] == [
            ("papa", "Papá", None, None, None),
            ("mama", "Mamá", None, None, None),
        ]
        assert unaccent == 1
        assert tuple(unaccented) == ("Neumonia", "i")
        assert analytes[0] >= 50 and analytes[1] == 0 and b12 == "sangre"
        assert diff == [], f"models and migrations disagree: {diff}"


def test_0024_gives_import_access_to_existing_admins_only(monkeypatch):
    """Ticket #96: the admins that exist at upgrade time keep the import area;
    members, and admins added later (column default), do not."""
    with PostgresContainer("postgres:17-alpine") as postgres:
        url = postgres.get_connection_url()
        monkeypatch.setenv("DATABASE_URL", url)
        cfg = _alembic_config()
        command.upgrade(cfg, "0023")
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (email, display_name, role) VALUES"
                        " ('op@example.com', 'Op', 'admin'),"
                        " ('ana@example.com', 'Ana', 'member')"
                    )
                )
            command.upgrade(cfg, "0024")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (email, display_name, role)"
                        " VALUES ('bea@example.com', 'Bea', 'admin')"
                    )
                )
                rows = conn.execute(
                    text("SELECT email, can_import FROM users ORDER BY email")
                ).all()
        finally:
            engine.dispose()
        assert [tuple(r) for r in rows] == [
            ("ana@example.com", False),
            ("bea@example.com", False),
            ("op@example.com", True),
        ]
