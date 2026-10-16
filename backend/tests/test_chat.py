"""Chat API and engine with a fake Anthropic client (never the real API).

Synthetic data only. The fake mimics the SDK surface the engine uses:
``client.beta.messages.stream(**params)`` (a context manager that yields
``text`` events and returns the final message) and
``client.messages.create(**params)`` for the title.
"""

import copy
import json
import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api.chat import chat_client_dep, session_factory_dep, storage_factory_dep
from app.core.config import Settings, get_settings
from app.main import app
from app.models.appointment import Appointment
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.chat import ChatMessage, ChatSession
from app.models.log_entry import LogEntry
from app.models.suggestion import Suggestion
from app.services.chat import engine, history, prompt, tools
from app.services.storage import StorageNotConfigured

SECRET_QUESTION = "¿Qué dijo la cardióloga sobre la presión arterial?"


# ── Fake client ──────────────────────────────────────────────────────────────


def usage(inp=100, out=20):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=50,
    )


def reply(*blocks, stop="end_turn"):
    """A final message: ``blocks`` are content dicts."""
    return {"content": list(blocks), "stop_reason": stop}


def text(value):
    return {"type": "text", "text": value}


def thinking(sig):
    return {"type": "thinking", "thinking": "", "signature": sig}


def tool_use(tid, name, args):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


class FakeStream:
    def __init__(self, client, step):
        self.client = client
        self.step = step

    def __enter__(self):
        # Whatever the engine holds while Claude generates is visible here.
        self.client.checked_out.append(self.client.pool_checkedout())
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        if self.step.get("raise_before"):
            raise self.step["raise_before"]
        for block in self.step["content"]:
            if block["type"] == "text":
                for piece in (
                    block["text"][: len(block["text"]) // 2],
                    block["text"][len(block["text"]) // 2 :],
                ):
                    if piece:
                        yield SimpleNamespace(type="text", text=piece)
        if self.step.get("raise_after"):
            raise self.step["raise_after"]

    def get_final_message(self):
        return SimpleNamespace(
            content=copy.deepcopy(self.step["content"]),
            stop_reason=self.step["stop_reason"],
            usage=usage(),
            model="claude-sonnet-5-5",
        )


class FakeClient:
    def __init__(self, steps, *, title="Presión arterial de Papá", title_error=None, pool=None):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.title_calls: list[dict] = []
        self.checked_out: list[int] = []
        self.pool_checkedout = pool or (lambda: 0)
        self._title = title
        self._title_error = title_error
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.messages = SimpleNamespace(create=self._create)

    def _stream(self, **params):
        self.calls.append(copy.deepcopy(params))
        step = self.steps.pop(0)
        if callable(step):
            step = step(params)
        return FakeStream(self, step)

    def _create(self, **params):
        self.title_calls.append(copy.deepcopy(params))
        if self._title_error:
            raise self._title_error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._title)], stop_reason="end_turn"
        )


def connection_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.example"))


# ── Fixtures and helpers ────────────────────────────────────────────────────


@pytest.fixture
def factory(test_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture
def use_client(factory, test_engine):
    """Install a fake client; returns a function taking the scripted steps."""
    app.dependency_overrides[session_factory_dep] = lambda: factory

    def install(steps, **kw):
        client = FakeClient(steps, pool=lambda: test_engine.pool.checkedout(), **kw)
        app.dependency_overrides[chat_client_dep] = lambda: client
        return client

    yield install
    app.dependency_overrides.pop(chat_client_dep, None)
    app.dependency_overrides.pop(session_factory_dep, None)


@pytest.fixture
def chat(auth_client, patients):
    resp = auth_client.post("/api/v1/chat/sessions", json={})
    assert resp.status_code == 201
    return resp.json()["id"]


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.split("\n\n"):
        lines = [ln for ln in chunk.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        name = next(ln[7:] for ln in lines if ln.startswith("event: "))
        data = json.loads(next(ln[6:] for ln in lines if ln.startswith("data: ")))
        events.append((name, data))
    return events


def send(client, session_id, message=SECRET_QUESTION):
    resp = client.post(f"/api/v1/chat/sessions/{session_id}/messages", json={"text": message})
    return resp


def rows(test_db, session_id):
    test_db.expire_all()
    return list(
        test_db.scalars(
            select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
        )
    )


# ── Auth, CSRF, configuration ───────────────────────────────────────────────


def test_requires_sign_in(db_client):
    assert db_client.get("/api/v1/chat/sessions").status_code == 401
    resp = db_client.post(
        "/api/v1/chat/sessions/1/messages", json={"text": "hola"}, headers={"X-Requested-With": "x"}
    )
    assert resp.status_code == 401


def test_writes_need_csrf_header(auth_client, chat, use_client):
    use_client([reply(text("hola"))])
    del auth_client.headers["X-Requested-With"]
    assert auth_client.post("/api/v1/chat/sessions", json={}).status_code == 403
    assert send(auth_client, chat).status_code == 403


def test_no_api_key_is_503(auth_client, chat):
    app.dependency_overrides[chat_client_dep] = lambda: None
    try:
        resp = send(auth_client, chat)
    finally:
        app.dependency_overrides.pop(chat_client_dep, None)
    assert resp.status_code == 503
    assert "clave" in resp.json()["detail"]


def test_default_client_dependency_without_key():
    assert chat_client_dep(Settings(anthropic_api_key="")) is None


def test_unknown_session_and_empty_text(auth_client, chat, use_client):
    use_client([])
    assert send(auth_client, 999).status_code == 404
    assert auth_client.get("/api/v1/chat/sessions/999").status_code == 404
    assert send(auth_client, chat, "   ").status_code == 422


# ── Sessions ─────────────────────────────────────────────────────────────────


def test_create_list_and_get_sessions(auth_client, test_db, member, patients):
    member_id = member.id
    first = auth_client.post("/api/v1/chat/sessions", json={"title": "  Citas  "}).json()
    second = auth_client.post("/api/v1/chat/sessions", json={}).json()
    assert first["title"] == "Citas" and second["title"] is None
    assert first["created_by"] == member_id and first["created_by_name"] == "Ana"
    assert first["generating"] is False
    listing = auth_client.get("/api/v1/chat/sessions").json()
    assert listing["total"] == 2
    assert [s["id"] for s in listing["items"]] == [second["id"], first["id"]]
    detail = auth_client.get(f"/api/v1/chat/sessions/{first['id']}").json()
    assert detail["messages"] == []
    audit = test_db.scalars(select(AuditLog).where(AuditLog.entity == "chat_session")).all()
    assert len(audit) == 2 and audit[0].user_id == member_id


# ── A turn ───────────────────────────────────────────────────────────────────


def test_simple_turn_streams_stores_and_titles(auth_client, test_db, chat, use_client, member):
    member_id = member.id
    client = use_client([reply(thinking("sig-1"), text("No encuentro esa consulta."))])
    resp = send(auth_client, chat)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"
    events = parse_sse(resp.text)
    names = [e[0] for e in events]
    assert names[0] == "start" and names[-1] == "done"
    assert names.count("delta") == 2
    assert "".join(d["text"] for n, d in events if n == "delta") == "No encuentro esa consulta."
    assert ("title", {"title": "Presión arterial de Papá"}) in events

    stored = rows(test_db, chat)
    assert [r.role for r in stored] == ["user", "assistant"]
    prompt_row, answer = stored
    assert events[0][1] == {"user_message_id": prompt_row.id}
    assert dict(events)["message"] == {"id": answer.id, "status": "complete"}
    assert prompt_row.author_user_id == member_id
    assert prompt_row.content[0]["text"].startswith("[Escribe: Ana · ")
    assert prompt_row.content[1] == {"type": "text", "text": SECRET_QUESTION}
    # The thinking block is stored exactly, for replay.
    assert answer.content == [thinking("sig-1"), text("No encuentro esa consulta.")]
    assert answer.meta["usage"]["input_tokens"] == 100
    assert answer.meta["stop_reason"] == "end_turn" and answer.meta["tool_rounds"] == 0

    session = test_db.get(ChatSession, chat)
    assert session.title == "Presión arterial de Papá"
    assert session.generating_since is None and session.last_message_at is not None

    (params,) = client.calls
    assert params["model"] == "claude-sonnet-5-5"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["system"][0]["text"] == prompt.SYSTEM_PROMPT
    assert params["tools"] == tools.definitions()
    assert params["thinking"] == {
        "type": "adaptive",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert params["cache_control"] == {"type": "ephemeral"}
    assert engine.BINDING_BETA in params["betas"] and params["fallbacks"] == "default"
    assert "tool_choice" not in params
    assert params["messages"] == [{"role": "user", "content": prompt_row.content}]

    # The title request: cheap model, first exchange only.
    (title_params,) = client.title_calls
    assert title_params["model"] == "claude-haiku-5-5"
    assert SECRET_QUESTION in title_params["messages"][0]["content"]

    detail = auth_client.get(f"/api/v1/chat/sessions/{chat}").json()
    assert [(m["kind"], m["text"]) for m in detail["messages"]] == [
        ("prompt", SECRET_QUESTION),
        ("assistant", "No encuentro esa consulta."),
    ]
    assert detail["messages"][0]["author_name"] == "Ana"
    assert test_db.scalars(select(AuditLog).where(AuditLog.entity == "chat_message")).one()


def test_session_detail_lists_files_owned_by_answers(auth_client, test_db, chat, use_client):
    use_client([reply(text("Aquí tienes el resumen."))])
    assert send(auth_client, chat).status_code == 200
    _prompt_row, answer = rows(test_db, chat)

    def file(name, mime, owner_type="chat_message", owner_id=answer.id):
        return Attachment(
            owner_type=owner_type,
            owner_id=owner_id,
            kind="documents",
            storage_key=f"documents/2026/{name}",
            filename=name,
            mime=mime,
            size=1234,
            sha256="0" * 64,
        )

    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    test_db.add_all(
        [
            file("resumen.pdf", "application/pdf"),
            file("labs.xlsx", xlsx),
            file("otro.pdf", "application/pdf", owner_type="log_entry", owner_id=answer.id),
            file("suelto.pdf", "application/pdf", owner_type=None, owner_id=None),
        ]
    )
    test_db.commit()

    prompt_msg, answer_msg = auth_client.get(f"/api/v1/chat/sessions/{chat}").json()["messages"]
    assert prompt_msg["attachments"] == []
    assert [(a["filename"], a["mime"], a["size"]) for a in answer_msg["attachments"]] == [
        ("resumen.pdf", "application/pdf", 1234),
        ("labs.xlsx", xlsx, 1234),
    ]
    assert set(answer_msg["attachments"][0]) == {"id", "filename", "mime", "size"}


def test_tool_loop_runs_tools_and_stores_the_round(
    auth_client, test_db, chat, use_client, patients
):
    papa_id = patients[0].id
    entry = LogEntry(
        patient_id=papa_id,
        entry_date=date(2025, 3, 12),
        entry_type="consulta_control",
        title="Control de presión",
    )
    test_db.add(entry)
    test_db.commit()
    entry_id = entry.id
    client = use_client(
        [
            reply(
                thinking("sig-a"),
                tool_use("toolu_1", "search_log_entries", {"q": "presion", "patient": "papa"}),
                tool_use("toolu_2", "get_current_meds", {"patient": "papa"}),
                stop="tool_use",
            ),
            reply(text("Según la consulta del 12/03/2025…")),
        ]
    )
    events = parse_sse(send(auth_client, chat).text)
    tool_events = [d for n, d in events if n == "tool"]
    assert tool_events == [
        {"name": "search_log_entries", "status": "Buscando en la bitácora…"},
        {"name": "get_current_meds", "status": "Consultando los medicamentos…"},
    ]
    stored = rows(test_db, chat)
    assert [(r.role, r.author_user_id is not None) for r in stored] == [
        ("user", True),
        ("assistant", False),
        ("user", False),
        ("assistant", False),
    ]
    results = stored[2].content
    assert [b["tool_use_id"] for b in results] == ["toolu_1", "toolu_2"]
    found = json.loads(results[0]["content"])
    assert found["items"][0]["id"] == entry_id and found["items"][0]["date"] == "2025-03-12"
    assert stored[3].meta["tool_rounds"] == 1

    second = client.calls[1]["messages"]
    assert second[1] == {"role": "assistant", "content": stored[1].content}
    assert second[2] == {"role": "user", "content": results}
    # The UI view shows the calls, not their results.
    detail = auth_client.get(f"/api/v1/chat/sessions/{chat}").json()
    assert [m["kind"] for m in detail["messages"]] == ["prompt", "assistant", "assistant"]
    assert [t["name"] for t in detail["messages"][1]["tools"]] == [
        "search_log_entries",
        "get_current_meds",
    ]


def test_iteration_cap_forces_an_answer(auth_client, test_db, chat, use_client):
    def step(params):
        if params.get("tool_choice") == {"type": "none"}:
            return reply(text("Con lo que encontré: nada registrado."))
        n = len(params["messages"])
        return reply(tool_use(f"toolu_{n}", "list_patients", {}), stop="tool_use")

    app.dependency_overrides[get_settings] = lambda: Settings(chat_max_tool_iterations=2)
    try:
        client = use_client([step, step, step])
        events = parse_sse(send(auth_client, chat).text)
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert len(client.calls) == 3
    assert [c.get("tool_choice") for c in client.calls] == [None, None, {"type": "none"}]
    # Same tools array on the forced answer (cached, bound prefix).
    assert client.calls[2]["tools"] == client.calls[0]["tools"]
    assert [n for n, _ in events].count("tool") == 2
    final = rows(test_db, chat)[-1]
    assert final.status == "complete" and final.meta["tool_rounds"] == 2


def test_history_is_replayed_exactly(auth_client, test_db, chat, use_client):
    use_client(
        [
            reply(thinking("sig-1"), tool_use("toolu_1", "list_patients", {}), stop="tool_use"),
            reply(thinking("sig-2"), text("Son Papá y Mamá.")),
        ]
    )
    send(auth_client, chat, "¿Quiénes son los pacientes?")
    first = [{"role": r.role, "content": r.content} for r in rows(test_db, chat)]
    client = use_client([reply(text("De nada."))], title="no se usa")
    events = parse_sse(send(auth_client, chat, "Gracias").text)
    assert "title" not in [n for n, _ in events]  # only after the first exchange
    assert client.title_calls == []
    messages = client.calls[0]["messages"]
    assert messages[:4] == first
    assert messages[4]["content"][1] == text("Gracias")
    assert len(messages) == 5


def test_error_mid_stream_keeps_partial_text_and_releases_lock(
    auth_client, test_db, chat, use_client
):
    use_client(
        [
            {
                "content": [text("Estoy revisando la bitácora y")],
                "stop_reason": "end_turn",
                "raise_after": connection_error(),
            }
        ]
    )
    events = parse_sse(send(auth_client, chat).text)
    names = [n for n, _ in events]
    assert "error" in names and names[-1] == "done"
    error = dict(events)["error"]
    assert error["code"] == "connection" and "conectar" in error["detail"]
    final = rows(test_db, chat)[-1]
    assert final.status == "interrupted" and final.meta["error"] == "connection"
    assert final.content == [text("Estoy revisando la bitácora y")]
    assert dict(events)["message"] == {"id": final.id, "status": "interrupted"}
    test_db.expire_all()
    assert test_db.get(ChatSession, chat).generating_since is None
    # A failed first exchange still gets a title: the question, shortened.
    assert test_db.get(ChatSession, chat).title.startswith("¿Qué dijo la cardióloga")

    # The next turn replays only the partial text.
    client = use_client([reply(text("Listo."))])
    send(auth_client, chat, "¿Y entonces?")
    replayed = client.calls[0]["messages"]
    assert replayed[1] == {"role": "assistant", "content": [text("Estoy revisando la bitácora y")]}


def test_error_before_any_text_stores_an_empty_interrupted_answer(
    auth_client, test_db, chat, use_client
):
    use_client([{"content": [], "stop_reason": "end_turn", "raise_before": connection_error()}])
    events = parse_sse(send(auth_client, chat).text)
    assert dict(events)["error"]["code"] == "connection"
    final = rows(test_db, chat)[-1]
    assert final.role == "assistant" and final.content == [] and final.status == "interrupted"
    client = use_client([reply(text("Ahora sí."))])
    send(auth_client, chat, "Otra vez")
    # The empty answer is not replayed; two family turns in a row are fine.
    assert [m["role"] for m in client.calls[0]["messages"]] == ["user", "user"]


def test_refusal_is_reported(auth_client, test_db, chat, use_client):
    use_client([reply(stop="refusal")])
    events = parse_sse(send(auth_client, chat).text)
    assert dict(events)["error"]["code"] == "refusal"
    assert rows(test_db, chat)[-1].status == "interrupted"


def test_concurrent_generation_is_409(auth_client, test_db, chat, use_client):
    use_client([reply(text("ok"))])
    session = test_db.get(ChatSession, chat)
    session.generating_since = datetime.now(UTC) - timedelta(minutes=1)
    test_db.commit()
    resp = send(auth_client, chat)
    assert resp.status_code == 409
    assert "Espera" in resp.json()["detail"]
    assert auth_client.get(f"/api/v1/chat/sessions/{chat}").json()["generating"] is True
    assert rows(test_db, chat) == []  # nothing stored

    # A stale lock (a crashed worker) does not block the session.
    session = test_db.get(ChatSession, chat)
    session.generating_since = datetime.now(UTC) - engine.LOCK_STALE - timedelta(minutes=1)
    test_db.commit()
    assert send(auth_client, chat).status_code == 200


def test_no_database_connection_is_held_while_claude_generates(
    auth_client, test_db, chat, use_client
):
    client = use_client(
        [
            reply(tool_use("toolu_1", "list_patients", {}), stop="tool_use"),
            reply(text("Listo.")),
        ]
    )
    assert send(auth_client, chat).status_code == 200
    assert client.checked_out == [0, 0]


def test_title_failure_falls_back_to_the_question(auth_client, test_db, chat, use_client):
    use_client([reply(text("Respuesta."))], title_error=RuntimeError("boom"))
    events = parse_sse(send(auth_client, chat, "Necesito la lista de citas de este mes " * 3).text)
    title = dict(events)["title"]["title"]
    assert title.startswith("Necesito la lista de citas") and title.endswith("…")
    assert len(title) <= 61


def test_message_content_never_reaches_the_logs(auth_client, chat, use_client, caplog):
    use_client(
        [
            reply(tool_use("toolu_1", "list_patients", {}), stop="tool_use"),
            reply(text("Respuesta con datos sintéticos.")),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="app"):
        send(auth_client, chat)
    assert "chat turn session=" in caplog.text
    assert SECRET_QUESTION not in caplog.text
    assert "datos sintéticos" not in caplog.text
    assert "Papá" not in caplog.text


# ── Units: replay, history cap, title ───────────────────────────────────────


def test_replayable_handles_fallback_blocks_and_unknown_types():
    content = [
        thinking("declined"),
        text("Parcial"),
        tool_use("toolu_x", "list_patients", {}),
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        thinking("ok"),
        text("Final"),
        {"type": "something_new", "x": 1},
    ]
    assert engine.replayable(content) == [text("Parcial"), thinking("ok"), text("Final")]


def _prompt(n):
    return history.StoredMessage("user", [text(f"[ctx {n}]"), text("x" * 100)], is_prompt=True)


def test_history_cap_drops_oldest_turns_and_strips_thinking():
    stored = []
    for n in range(5):
        stored.append(_prompt(n))
        stored.append(history.StoredMessage("assistant", [thinking(f"s{n}"), text("y" * 100)]))
    stored.append(_prompt(5))
    full, omitted = history.build_messages(stored, max_chars=10_000)
    assert omitted == 0 and len(full) == 11 and full[1]["content"][0] == thinking("s0")

    capped, omitted = history.build_messages(stored, max_chars=800)
    assert omitted > 0 and omitted % 2 == 0  # cut at a family message
    assert capped[0]["content"][0] == history.omitted_note(omitted)
    assert capped[0]["role"] == "user"
    assert all(b["type"] != "thinking" for m in capped for b in m["content"])
    assert capped[-1]["content"][-1] == text("x" * 100)


def test_history_drops_unanswered_tool_use_and_interrupted_non_text():
    stored = [
        _prompt(0),
        history.StoredMessage(
            "assistant",
            [thinking("s"), text("Busco"), tool_use("t1", "list_patients", {})],
            status="interrupted",
        ),
        _prompt(1),
        history.StoredMessage("assistant", [tool_use("t2", "list_patients", {})]),
        _prompt(2),
    ]
    messages, _ = history.build_messages(stored)
    assert messages[1] == {"role": "assistant", "content": [text("Busco")]}
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "user"]


def test_context_line_is_panama_time():
    line = prompt.context_line("Ana", datetime(2026, 10, 9, 15, 32, tzinfo=UTC))
    assert line == "[Escribe: Ana · viernes 9 de octubre de 2026, 10:32 (hora de Panamá)]"


def test_clean_title():
    from app.services.chat.title import clean_title, fallback_title

    assert clean_title('Título: "Citas de Mamá".\nOtra línea') == "Citas de Mamá"
    assert fallback_title("") == "Nueva conversación"
    assert fallback_title("corto") == "corto"


def test_storage_failure_reports_and_releases_the_lock(
    auth_client, test_db, chat, use_client, monkeypatch
):
    def broken(self, new):
        raise RuntimeError("db down")

    monkeypatch.setattr(engine.TurnRunner, "_persist", broken)
    use_client([reply(text("Respuesta."))])
    events = parse_sse(send(auth_client, chat).text)
    assert dict(events)["error"]["code"] == "storage"
    assert "message" not in dict(events)
    test_db.expire_all()
    assert test_db.get(ChatSession, chat).generating_since is None


def test_a_turn_that_lost_its_lock_stores_nothing_and_leaves_the_new_lock(
    auth_client, test_db, chat, use_client, factory
):
    other = datetime.now(UTC) + timedelta(seconds=5)

    def steal(params):
        # Another turn takes the (stale) lock while this one is generating.
        with factory() as db:
            db.get(ChatSession, chat).generating_since = other
            db.commit()
        return reply(text("Respuesta tardía."))

    use_client([steal])
    events = parse_sse(send(auth_client, chat).text)
    assert dict(events)["error"]["code"] == "lock_lost"
    assert "message" not in dict(events)
    assert [r.role for r in rows(test_db, chat)] == ["user"]  # nothing after
    assert test_db.get(ChatSession, chat).generating_since == other  # not cleared


def test_release_lock_is_compare_and_clear(test_db, chat):
    token = engine.acquire_lock(test_db, chat)
    test_db.commit()
    assert token is not None and engine.acquire_lock(test_db, chat) is None
    engine.release_lock(test_db, chat, token - timedelta(seconds=1))
    test_db.commit()
    test_db.expire_all()
    assert test_db.get(ChatSession, chat).generating_since == token
    engine.release_lock(test_db, chat, token)
    test_db.commit()
    test_db.expire_all()
    assert test_db.get(ChatSession, chat).generating_since is None


def test_lock_stale_exceeds_the_worst_case_turn():
    from app.services.chat import title as title_svc

    worst = (
        engine.TURN_DEADLINE_SECONDS
        + (engine.MAX_RETRIES + 1) * 5 * 60  # one request streaming MAX_TOKENS
        + (engine.MAX_RETRIES + 1) * title_svc.TITLE_TIMEOUT_SECONDS
    )
    assert engine.LOCK_STALE.total_seconds() > worst


def test_deadline_is_checked_before_running_tools(
    auth_client, test_db, chat, use_client, monkeypatch
):
    ticks = iter([0.0, 0.0])
    monkeypatch.setattr(engine, "_clock", lambda: next(ticks, 10.0**6))
    client = use_client(
        [
            reply(
                text("Voy a buscar."),
                tool_use("toolu_1", "list_patients", {}),
                stop="tool_use",
            )
        ]
    )
    events = parse_sse(send(auth_client, chat).text)
    assert "tool" not in [n for n, _ in events]
    assert dict(events)["error"]["code"] == "deadline"
    assert len(client.calls) == 1
    final = rows(test_db, chat)[-1]
    assert final.content == [text("Voy a buscar.")] and final.status == "interrupted"


def test_tool_time_failure_does_not_store_the_text_twice(
    auth_client, test_db, chat, use_client, factory
):
    use_client(
        [
            reply(
                text("Busco en la bitácora."),
                tool_use("toolu_1", "list_patients", {}),
                stop="tool_use",
            )
        ]
    )
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:  # 1: history load, 2: the tool's session
            raise RuntimeError("pool exhausted")
        return factory()

    app.dependency_overrides[session_factory_dep] = lambda: flaky
    events = parse_sse(send(auth_client, chat).text)
    assert dict(events)["error"]["code"] == "internal"
    stored = rows(test_db, chat)
    assert [r.role for r in stored] == ["user", "assistant"]
    texts = [b["text"] for r in stored[1:] for b in r.content if b["type"] == "text"]
    assert texts == ["Busco en la bitácora."]
    assert stored[1].status == "interrupted"
    test_db.expire_all()
    assert test_db.get(ChatSession, chat).generating_since is None


# ── File tools (#33): SSE ``file`` event and linking at persist ─────────────

XLSX_INPUT = {
    "title": "Glucosa de Papá",
    "sheets": [
        {
            "name": "Glucosa",
            "columns": ["Fecha", "Valor"],
            "rows": [["2025-03-12", 98], ["2025-05-02", "Valor secreto sintético"]],
        }
    ],
}


@pytest.fixture
def file_storage(storage):
    app.dependency_overrides[storage_factory_dep] = lambda: lambda: storage
    yield storage
    app.dependency_overrides.pop(storage_factory_dep, None)


def test_generated_file_is_announced_and_owned_by_the_answer(
    auth_client, test_db, chat, use_client, file_storage, member, caplog
):
    member_id = member.id
    use_client(
        [
            reply(tool_use("toolu_1", "generate_xlsx", XLSX_INPUT), stop="tool_use"),
            reply(text("Listo, te dejé el Excel.")),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="app"):
        events = parse_sse(send(auth_client, chat).text)
    names = [n for n, _ in events]
    assert names.index("tool") < names.index("file") < names.index("message")
    file_event = dict(events)["file"]
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert set(file_event) == {"id", "filename", "mime", "size"}
    assert file_event["mime"] == xlsx and file_event["filename"].startswith("glucosa-de-papa-")
    assert dict(events)["tool"]["status"] == "Generando el Excel…"

    stored = rows(test_db, chat)
    final = stored[-1]
    assert final.id == dict(events)["message"]["id"]
    assert final.meta["attachment_ids"] == [file_event["id"]]
    att = test_db.get(Attachment, file_event["id"])
    assert (att.owner_type, att.owner_id) == ("chat_message", final.id)
    links = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "attachment", AuditLog.action == "link")
    ).all()
    assert [(a.entity_id, a.user_id) for a in links] == [(str(att.id), member_id)]
    # The tool result tells Claude the file's name and id.
    result = json.loads(stored[2].content[0]["content"])
    assert result["file_id"] == att.id and result["filename"] == att.filename

    detail = auth_client.get(f"/api/v1/chat/sessions/{chat}").json()
    answers = [m for m in detail["messages"] if m["kind"] == "assistant"]
    assert answers[0]["attachments"] == []  # the tool round's row
    assert answers[-1]["attachments"] == [file_event]
    assert "Valor secreto" not in caplog.text and "Glucosa" not in caplog.text


def test_files_of_an_interrupted_turn_belong_to_the_interrupted_answer(
    auth_client, test_db, chat, use_client, file_storage
):
    use_client(
        [
            reply(tool_use("toolu_1", "generate_xlsx", XLSX_INPUT), stop="tool_use"),
            {"content": [], "stop_reason": "end_turn", "raise_before": connection_error()},
        ]
    )
    events = parse_sse(send(auth_client, chat).text)
    assert dict(events)["error"]["code"] == "connection"
    final = rows(test_db, chat)[-1]
    assert final.status == "interrupted"
    att = test_db.get(Attachment, dict(events)["file"]["id"])
    assert (att.owner_type, att.owner_id) == ("chat_message", final.id)
    detail = auth_client.get(f"/api/v1/chat/sessions/{chat}").json()
    assert detail["messages"][-1]["attachments"][0]["id"] == att.id


def test_file_tool_without_storage_is_an_error_result(auth_client, test_db, chat, use_client):
    def missing():
        raise StorageNotConfigured("no keys")

    app.dependency_overrides[storage_factory_dep] = lambda: missing
    try:
        use_client(
            [
                reply(tool_use("toolu_1", "generate_xlsx", XLSX_INPUT), stop="tool_use"),
                reply(text("No pude crear el archivo.")),
            ]
        )
        events = parse_sse(send(auth_client, chat).text)
    finally:
        app.dependency_overrides.pop(storage_factory_dep, None)
    assert "file" not in dict(events)
    stored = rows(test_db, chat)
    result = stored[2].content[0]
    assert result["is_error"] is True and result["content"] == tools.STORAGE_UNAVAILABLE
    assert "attachment_ids" not in stored[-1].meta


# ── Ready-made documents (#34): the CIP reaches the file, never Claude ──────


def test_emergency_card_tool_file_is_linked_and_claude_never_sees_the_cip(
    auth_client, test_db, chat, use_client, file_storage, patients, caplog
):
    papa = patients[0]
    papa.full_name = "Nombre Sintético Privado"
    papa.id_number = "8-888-8888"
    test_db.commit()
    client = use_client(
        [
            reply(tool_use("toolu_1", "emergency_card", {"patient": "papa"}), stop="tool_use"),
            reply(text("Listo, te dejé la tarjeta.")),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="app"):
        events = parse_sse(send(auth_client, chat).text)
    file_event = dict(events)["file"]
    assert file_event["filename"].startswith("tarjeta-emergencia-papa-")
    assert dict(events)["tool"]["status"] == "Preparando la tarjeta de emergencia…"
    final = rows(test_db, chat)[-1]
    att = test_db.get(Attachment, file_event["id"])
    assert (att.owner_type, att.owner_id) == ("chat_message", final.id)
    # What went back to Claude (the second request) carries neither the CIP nor the name.
    second = json.dumps(client.calls[1]["messages"], ensure_ascii=False)
    assert "tarjeta-emergencia-papa-" in second
    assert "8-888-8888" not in second and "Privado" not in second
    assert "8-888-8888" not in caplog.text and "Privado" not in caplog.text


# ── Proposals (#41): a suggestion in «Pendientes», never a record ───────────

PROPOSAL = {
    "kind": "create_appointment",
    "patient": "mama",
    "payload": {"date": "2030-03-04", "time": "10:00", "reason": "Control sintético"},
    "rationale": "Ana avisó una cita sintética.",
}


def test_proposal_is_announced_listed_on_the_answer_and_never_applied(
    auth_client, test_db, chat, use_client, member, patients, caplog
):
    member_id, mama_id = member.id, patients[1].id
    use_client(
        [
            reply(tool_use("toolu_1", "propose_change", PROPOSAL), stop="tool_use"),
            reply(text("Propuse la cita; queda en Pendientes hasta que alguien la apruebe.")),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="app"):
        events = parse_sse(send(auth_client, chat, "Mamá tiene cita el 4 de marzo").text)
    names = [n for n, _ in events]
    assert names.index("tool") < names.index("suggestion") < names.index("message")
    assert dict(events)["tool"]["status"] == "Preparando una propuesta para Pendientes…"
    proposal = dict(events)["suggestion"]
    assert proposal["kind"] == "create_appointment" and proposal["patient_id"] == mama_id
    assert proposal["summary"] == "04/03/2030 · 10:00 · Control sintético"

    test_db.expire_all()
    row = test_db.get(Suggestion, proposal["id"])
    assert (row.source, row.status, row.chat_session_id) == ("chat", "pending", chat)
    assert row.created_by == member_id
    assert test_db.scalar(select(func.count()).select_from(Appointment)) == 0
    final = rows(test_db, chat)[-1]
    assert final.meta["suggestion_ids"] == [row.id]
    assert "Control sintético" not in caplog.text and "sintética" not in caplog.text

    detail = auth_client.get(f"/api/v1/chat/sessions/{chat}").json()
    answers = [m for m in detail["messages"] if m["kind"] == "assistant"]
    assert answers[0]["suggestions"] == []  # the tool round's row
    assert answers[-1]["suggestions"] == [{**proposal, "status": "pending"}]

    # «Pendientes» shows where it came from; the card follows its status.
    listed = auth_client.get("/api/v1/suggestions").json()["items"]
    assert [(s["id"], s["source"], s["chat_session_id"], s["created_by_name"]) for s in listed] == [
        (row.id, "chat", chat, "Ana")
    ]
    assert auth_client.post(f"/api/v1/suggestions/{row.id}/reject", json={}).status_code == 200
    detail = auth_client.get(f"/api/v1/chat/sessions/{chat}").json()
    assert detail["messages"][-1]["suggestions"][0]["status"] == "rejected"


def test_invalid_proposal_goes_back_to_claude_as_an_error(auth_client, test_db, chat, use_client):
    bad = {**PROPOSAL, "payload": {"time": "10:00"}}
    client = use_client(
        [
            reply(tool_use("toolu_1", "propose_change", bad), stop="tool_use"),
            reply(text("Me falta la fecha de la cita.")),
        ]
    )
    events = parse_sse(send(auth_client, chat).text)
    assert "suggestion" not in [n for n, _ in events]
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and '"date"' in result["content"]
    assert test_db.scalar(select(func.count()).select_from(Suggestion)) == 0
    assert "suggestion_ids" not in rows(test_db, chat)[-1].meta
