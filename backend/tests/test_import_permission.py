"""Ticket #96: the WhatsApp import area (``/imports/whatsapp``,
``/analysis-runs``, ``/extension/token``) needs an admin **with**
``users.can_import``; a plain admin gets 403. Synthetic users only."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models.extension_token import ExtensionToken
from app.models.user import User
from app.services import extension_tokens
from scripts import users as users_script

IMPORTS = "/api/v1/imports/whatsapp"
RUNS = "/api/v1/analysis-runs"
TOKEN_URL = "/api/v1/extension/token"

# Every guarded route (method, path, JSON body). Ids need not exist: the
# permission check runs before any lookup.
IMPORT_ROUTES = [
    ("GET", IMPORTS, None),
    ("POST", IMPORTS, {"mode": "daily", "filename": "a.zip", "size": 10}),
    ("GET", f"{IMPORTS}/last", None),
    ("GET", f"{IMPORTS}/media-missing", None),
    ("GET", f"{IMPORTS}/1", None),
    ("PUT", f"{IMPORTS}/1/chunks/0", None),
    ("POST", f"{IMPORTS}/1/complete", None),
    ("DELETE", f"{IMPORTS}/1", None),
    ("POST", f"{IMPORTS}/1/messages/1/media", None),
    ("POST", f"{IMPORTS}/1/media/complete", None),
]
COOKIE_ONLY_ROUTES = [
    ("POST", f"{RUNS}/estimate", {"patient_id": 1}),
    ("GET", RUNS, None),
    ("POST", RUNS, {"patient_id": 1}),
    ("GET", f"{RUNS}/1", None),
    ("POST", f"{RUNS}/1/stop", None),
    ("POST", f"{RUNS}/1/resume", None),
    ("POST", f"{RUNS}/1/reject-pending", None),
    ("GET", TOKEN_URL, None),
    ("POST", TOKEN_URL, None),
    ("DELETE", TOKEN_URL, None),
]
ALL_ROUTES = IMPORT_ROUTES + COOKIE_ONLY_ROUTES


def _call(client, method, path, body):
    if method == "PUT":
        return client.put(path, content=b"x", headers={"Content-Type": "application/octet-stream"})
    return client.request(method, path, json=body)


@pytest.fixture
def plain_admin(auth_client, member, test_db):
    """An admin without the import permission (a sibling, say)."""
    member.role = "admin"
    member.can_import = False
    test_db.commit()
    return auth_client


@pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES)
def test_admin_without_permission_is_refused(plain_admin, method, path, body):
    resp = _call(plain_admin, method, path, body)
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES)
def test_member_with_a_stray_flag_is_refused(auth_client, member, test_db, method, path, body):
    # The flag counts only together with the admin role.
    member.can_import = True
    test_db.commit()
    assert _call(auth_client, method, path, body).status_code == 403


def test_import_admin_is_let_in(auth_client, member, test_db):
    member.role = "admin"
    member.can_import = True
    test_db.commit()
    assert auth_client.get(IMPORTS).status_code == 200
    assert auth_client.get(RUNS).status_code == 200
    assert auth_client.get(TOKEN_URL).status_code == 200


def test_me_exposes_the_effective_permission(auth_client, member, test_db):
    assert auth_client.get("/api/v1/auth/me").json()["can_import"] is False
    member.role = "admin"
    test_db.commit()
    assert auth_client.get("/api/v1/auth/me").json()["can_import"] is False
    member.can_import = True
    test_db.commit()
    body = auth_client.get("/api/v1/auth/me").json()
    assert body["can_import"] is True and body["is_admin"] is True


def test_existing_extension_key_stops_working_without_the_permission(auth_client, member, test_db):
    member.role = "admin"
    member.can_import = True
    test_db.commit()
    resp = auth_client.post(TOKEN_URL)
    assert resp.status_code == 201
    token = resp.json()["token"]
    with TestClient(app) as ext:
        ext.headers.update({"Authorization": f"Bearer {token}", "X-Requested-With": "x"})
        assert ext.get(IMPORTS).status_code == 200
        member.can_import = False
        test_db.commit()
        for method, path, body in IMPORT_ROUTES:
            assert _call(ext, method, path, body).status_code == 403, (method, path)
        member.can_import = True
        test_db.commit()
        assert ext.get(IMPORTS).status_code == 200


# ── python -m scripts.users import-access ───────────────────────────────────


@pytest.fixture
def cli_db(test_engine, test_db, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(users_script, "SessionLocal", sessionmaker(bind=test_engine))
    return test_db


def test_script_sets_and_clears_import_access(cli_db, capsys):
    assert users_script.main(["add", "bea@example.com", "--name", "Bea", "--admin"]) == 0
    user = cli_db.scalar(select(User))
    assert user.is_admin and user.can_import is False  # a new admin starts without it
    assert users_script.main(["list"]) == 0
    assert "bea@example.com\tBea\tadmin\tactive\t-" in capsys.readouterr().out

    assert users_script.main(["import-access", "Bea@Example.com", "on"]) == 0
    cli_db.expire_all()
    assert cli_db.scalar(select(User)).can_import is True
    assert users_script.main(["list"]) == 0
    assert "bea@example.com\tBea\tadmin\tactive\timports" in capsys.readouterr().out

    extension_tokens.regenerate(cli_db, cli_db.scalar(select(User)))
    cli_db.commit()
    assert users_script.main(["import-access", "bea@example.com", "off"]) == 0
    cli_db.expire_all()
    assert cli_db.scalar(select(User)).can_import is False
    # «off» also revokes the extension key.
    assert cli_db.scalar(select(ExtensionToken)).revoked_at is not None


def test_script_import_access_errors(cli_db):
    assert users_script.main(["import-access", "missing@example.com", "on"]) == 1
    assert users_script.main(["add", "carlos@example.com"]) == 0
    # Only an admin can get it.
    assert users_script.main(["import-access", "carlos@example.com", "on"]) == 1
