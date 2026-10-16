"""``python -m scripts.users`` (allowlist CLI). Synthetic users only."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.user import Session, User
from app.services.auth import utcnow
from scripts import users as users_script


@pytest.fixture
def cli_db(test_engine, test_db, monkeypatch):
    """Point the script's SessionLocal at the test database."""
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(users_script, "SessionLocal", sessionmaker(bind=test_engine))
    return test_db


def test_add_list_deactivate(cli_db, capsys):
    assert users_script.main(["add", "Bea@Example.com", "--name", "Bea", "--admin"]) == 0
    user = cli_db.scalar(select(User))
    assert (user.email, user.display_name, user.role, user.is_active) == (
        "bea@example.com",
        "Bea",
        "admin",
        True,
    )
    cli_db.add(
        Session(
            user_id=user.id,
            token_hash="x" * 64,
            expires_at=utcnow() + timedelta(days=1),
            last_seen_at=utcnow(),
        )
    )
    cli_db.commit()

    assert users_script.main(["list"]) == 0
    assert "bea@example.com\tBea\tadmin\tactive" in capsys.readouterr().out

    assert users_script.main(["deactivate", "bea@example.com"]) == 0
    cli_db.expire_all()
    assert cli_db.scalar(select(User)).is_active is False
    assert cli_db.scalar(select(Session)) is None

    # add reactivates an existing user
    assert users_script.main(["add", "bea@example.com"]) == 0
    cli_db.expire_all()
    assert cli_db.scalar(select(User)).is_active is True


def test_add_defaults_name_and_member_role(cli_db):
    assert users_script.main(["add", "carlos@example.com"]) == 0
    user = cli_db.scalar(select(User))
    assert (user.display_name, user.role) == ("carlos", "member")


def test_errors(cli_db):
    assert users_script.main(["add", "not-an-email"]) == 1
    assert users_script.main(["deactivate", "missing@example.com"]) == 1
