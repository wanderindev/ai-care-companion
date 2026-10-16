"""Archive, restore and delete chat sessions (#99). Synthetic data only."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.api.chat import storage_factory_dep
from app.core.config import get_settings
from app.main import app
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.chat import ChatMessage, ChatSession
from app.models.suggestion import Suggestion
from app.models.user import User
from app.services.auth import create_session

SECRET = "Pregunta sintética sobre la presión de Papá"
TITLE = "Presión de Papá"


def sign_in_as(client, test_db, user):
    settings = get_settings()
    _, token = create_session(test_db, settings, user, "pytest")
    client.cookies.set(settings.session_cookie_name, token)


@pytest.fixture
def other(test_db):
    user = User(email="luis@example.com", display_name="Luis")
    test_db.add(user)
    test_db.commit()
    return user


@pytest.fixture
def admin(test_db):
    user = User(email="eva@example.com", display_name="Eva", role="admin")
    test_db.add(user)
    test_db.commit()
    return user


@pytest.fixture
def chat(auth_client):
    resp = auth_client.post("/api/v1/chat/sessions", json={"title": TITLE})
    assert resp.status_code == 201
    return resp.json()["id"]


def ids(listing):
    return [s["id"] for s in listing["items"]]


def audits(test_db, entity):
    test_db.expire_all()
    return list(
        test_db.scalars(select(AuditLog).where(AuditLog.entity == entity).order_by(AuditLog.id))
    )


# ── Archive / restore ────────────────────────────────────────────────────────


def test_archive_hides_from_default_list_and_restore_brings_it_back(
    auth_client, test_db, chat, other
):
    keep = auth_client.post("/api/v1/chat/sessions", json={}).json()["id"]
    # Sessions are shared: a member who did not start it may archive it.
    sign_in_as(auth_client, test_db, other)
    resp = auth_client.patch(f"/api/v1/chat/sessions/{chat}", json={"archived": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["archived_at"] is not None and body["can_delete"] is False

    active = auth_client.get("/api/v1/chat/sessions").json()
    assert ids(active) == [keep] and active["total"] == 1
    archived = auth_client.get("/api/v1/chat/sessions?archived=true").json()
    assert ids(archived) == [chat] and archived["total"] == 1
    # The open session still loads, marked archived.
    assert auth_client.get(f"/api/v1/chat/sessions/{chat}").json()["archived_at"] is not None

    # Archiving twice changes nothing (no second audit row).
    assert (
        auth_client.patch(f"/api/v1/chat/sessions/{chat}", json={"archived": True}).status_code
        == 200
    )
    resp = auth_client.patch(f"/api/v1/chat/sessions/{chat}", json={"archived": False})
    assert resp.status_code == 200 and resp.json()["archived_at"] is None
    assert set(ids(auth_client.get("/api/v1/chat/sessions").json())) == {chat, keep}

    updates = [a for a in audits(test_db, "chat_session") if a.action == "update"]
    assert len(updates) == 2
    assert updates[0].user_id == other.id
    assert updates[0].before == {"archived_at": None}
    assert updates[0].after["archived_at"] is not None
    assert updates[1].after == {"archived_at": None}


def test_archive_unknown_session_csrf_and_sign_in(auth_client, db_client, chat):
    assert (
        auth_client.patch("/api/v1/chat/sessions/999999", json={"archived": True}).status_code
        == 404
    )
    assert auth_client.patch(f"/api/v1/chat/sessions/{chat}", json={}).status_code == 422
    del auth_client.headers["X-Requested-With"]
    assert (
        auth_client.patch(f"/api/v1/chat/sessions/{chat}", json={"archived": True}).status_code
        == 403
    )
    assert auth_client.delete(f"/api/v1/chat/sessions/{chat}").status_code == 403
    auth_client.cookies.clear()
    auth_client.headers["X-Requested-With"] = "fetch"
    assert auth_client.delete(f"/api/v1/chat/sessions/{chat}").status_code == 401


def test_archive_and_delete_refused_while_generating(auth_client, test_db, chat):
    session = test_db.get(ChatSession, chat)
    session.generating_since = datetime.now(UTC)
    test_db.commit()
    resp = auth_client.patch(f"/api/v1/chat/sessions/{chat}", json={"archived": True})
    assert resp.status_code == 409
    assert auth_client.delete(f"/api/v1/chat/sessions/{chat}").status_code == 409
    test_db.expire_all()
    assert test_db.get(ChatSession, chat).archived_at is None


# ── Delete: permissions ──────────────────────────────────────────────────────


def test_delete_needs_creator_or_admin(auth_client, test_db, chat, other, admin):
    second = auth_client.post("/api/v1/chat/sessions", json={}).json()["id"]
    assert auth_client.get("/api/v1/chat/sessions").json()["items"][0]["can_delete"] is True

    sign_in_as(auth_client, test_db, other)
    assert auth_client.get(f"/api/v1/chat/sessions/{chat}").json()["can_delete"] is False
    assert auth_client.delete(f"/api/v1/chat/sessions/{chat}").status_code == 403
    test_db.expire_all()
    assert test_db.get(ChatSession, chat) is not None

    sign_in_as(auth_client, test_db, admin)
    assert auth_client.get(f"/api/v1/chat/sessions/{second}").json()["can_delete"] is True
    assert auth_client.delete(f"/api/v1/chat/sessions/{second}").status_code == 204
    assert auth_client.delete(f"/api/v1/chat/sessions/{second}").status_code == 404


def test_creator_deletes_an_archived_session(auth_client, test_db, chat):
    assert (
        auth_client.patch(f"/api/v1/chat/sessions/{chat}", json={"archived": True}).status_code
        == 200
    )
    assert auth_client.delete(f"/api/v1/chat/sessions/{chat}").status_code == 204
    assert auth_client.get(f"/api/v1/chat/sessions/{chat}").status_code == 404
    assert auth_client.get("/api/v1/chat/sessions?archived=true").json()["total"] == 0


# ── Delete: messages, suggestions, files, audit ─────────────────────────────


@pytest.fixture
def store(storage):
    app.dependency_overrides[storage_factory_dep] = lambda: lambda: storage
    yield storage
    app.dependency_overrides.pop(storage_factory_dep, None)


def test_delete_cascades_messages_keeps_suggestions_and_handles_files(
    auth_client, test_db, chat, member, patients, store
):
    prompt = ChatMessage(
        session_id=chat,
        role="user",
        content=[{"type": "text", "text": SECRET}],
        author_user_id=member.id,
    )
    answer = ChatMessage(
        session_id=chat, role="assistant", content=[{"type": "text", "text": "Respuesta"}]
    )
    test_db.add_all([prompt, answer])
    test_db.flush()

    def file(name, owner_type="chat_message", owner_id=answer.id, key=None):
        return Attachment(
            owner_type=owner_type,
            owner_id=owner_id,
            kind="documents",
            storage_key=key or f"documents/2026/{name}",
            filename=name,
            mime="application/pdf",
            size=4,
            sha256="0" * 64,
        )

    only = file("solo.pdf")
    shared = file("compartido.pdf")
    # A copy made when a suggestion was approved: same stored object.
    copy = file("copia.pdf", owner_type="log_entry", owner_id=1, key=shared.storage_key)
    unrelated = file("otro.pdf", owner_type="log_entry", owner_id=answer.id)
    test_db.add_all([only, shared, copy, unrelated])
    suggestion = Suggestion(
        kind="create_entry",
        source="chat",
        patient_id=patients[0].id,
        payload={"title": "Sintética"},
        chat_session_id=chat,
        created_by=member.id,
    )
    test_db.add(suggestion)
    test_db.commit()
    for key in (only.storage_key, shared.storage_key, unrelated.storage_key):
        store.put(key, b"%PDF", "application/pdf")
    ids_ = {
        "prompt": prompt.id,
        "answer": answer.id,
        "only": only.id,
        "shared": shared.id,
        "copy": copy.id,
        "unrelated": unrelated.id,
        "suggestion": suggestion.id,
    }

    assert auth_client.delete(f"/api/v1/chat/sessions/{chat}").status_code == 204

    test_db.expire_all()
    assert test_db.get(ChatSession, chat) is None
    assert test_db.get(ChatMessage, ids_["prompt"]) is None
    assert test_db.get(ChatMessage, ids_["answer"]) is None
    kept = test_db.get(Suggestion, ids_["suggestion"])
    assert kept is not None and kept.chat_session_id is None and kept.status == "pending"
    # Sole reference: row and object gone. Shared object: row unlinked, object kept.
    assert test_db.get(Attachment, ids_["only"]) is None
    assert not store.exists(only.storage_key)
    unlinked = test_db.get(Attachment, ids_["shared"])
    assert unlinked.owner_type is None and unlinked.owner_id is None
    assert store.exists(unlinked.storage_key)
    assert test_db.get(Attachment, ids_["copy"]).owner_type == "log_entry"
    assert test_db.get(Attachment, ids_["unrelated"]).owner_type == "log_entry"
    assert store.exists("documents/2026/otro.pdf")

    (row,) = [a for a in audits(test_db, "chat_session") if a.action == "delete"]
    assert row.user_id == member.id and row.entity_id == str(chat)
    assert row.before["message_count"] == 2
    assert row.before["deleted_attachment_ids"] == [ids_["only"]]
    assert row.before["unlinked_attachment_ids"] == [ids_["shared"]]
    assert row.before["suggestion_ids"] == [ids_["suggestion"]]
    assert "title" not in row.before
    assert SECRET not in str(row.before) and TITLE not in str(row.before)
    att_audit = {(a.action, a.entity_id) for a in audits(test_db, "attachment")}
    assert att_audit == {("delete", str(ids_["only"])), ("unlink", str(ids_["shared"]))}


def test_delete_without_storage_still_deletes(auth_client, test_db, chat):
    answer = ChatMessage(session_id=chat, role="assistant", content=[{"type": "text", "text": "R"}])
    test_db.add(answer)
    test_db.flush()
    test_db.add(
        Attachment(
            owner_type="chat_message",
            owner_id=answer.id,
            kind="documents",
            storage_key="documents/2026/x.pdf",
            filename="x.pdf",
            mime="application/pdf",
            size=4,
            sha256="1" * 64,
        )
    )
    test_db.commit()

    def broken():
        raise RuntimeError("no storage")

    app.dependency_overrides[storage_factory_dep] = lambda: broken
    try:
        assert auth_client.delete(f"/api/v1/chat/sessions/{chat}").status_code == 204
    finally:
        app.dependency_overrides.pop(storage_factory_dep, None)
    test_db.expire_all()
    assert test_db.get(ChatSession, chat) is None
