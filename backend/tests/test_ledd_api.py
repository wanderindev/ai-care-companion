"""LEDD over the database and the API (ticket #133): the list's breakdown, the
save's before → after, versions, the printed sheet, the chat tool, and the AI
classification of unknown names (fake client; synthetic data only)."""

import io
import json
import logging
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.audit import AuditLog
from app.models.job import Job
from app.models.ledd import LeddSubstance
from app.services import jobs, ledd_substances
from app.services.chat import tools


def url(patient, suffix=""):
    return f"/api/v1/patients/{patient.id}/meds{suffix}"


def med(name, slot="desayuno", quantity="1", **kw):
    return {"name": name, "slot": slot, "quantity": quantity, **kw}


# A synthetic list built like the family's sheet: 300 + 60 + 25 = 385 mg/day by hand.
FAMILY_STYLE = [
    med("Prolopa", "desayuno", strength="125 mg"),
    med("Prolopa", "almuerzo", strength="125 mg"),
    med("Prolopa", "noche", strength="125 mg"),
    med("Rotigotina", "desayuno", strength="2 mg", dose_note="(parche)"),
    med("Parmital", "noche", quantity="½", strength="0.5 mg", dose_note="(0.25 mg total)"),
    med("Medicina Común", "cena", strength="50 mg"),
]


class FakeMessages:
    def __init__(self):
        self.calls: list[dict] = []
        self.output: dict | str = {
            "is_dopaminergic": True,
            "mechanism": "dopamine_agonist",
            "factor": 5.0,
            "generic_name": "sustancia nueva",
            "source": "Jost 2023",
            "confidence": "medium",
        }
        self.stop_reason = "end_turn"
        self.error: Exception | None = None

    def create(self, **params):
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        text = self.output if isinstance(self.output, str) else json.dumps(self.output)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=300,
                output_tokens=40,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


@pytest.fixture
def papa(patients):
    return patients[0]


@pytest.fixture
def fake(monkeypatch, test_engine):
    state = SimpleNamespace(
        messages=FakeMessages(),
        settings=Settings(anthropic_api_key="test-key", classification_model="claude-haiku-5-5"),
        factory=sessionmaker(bind=test_engine, autoflush=False),
    )
    monkeypatch.setattr(
        ledd_substances,
        "make_client",
        lambda key: SimpleNamespace(beta=SimpleNamespace(messages=state.messages)),
    )
    monkeypatch.setattr(ledd_substances, "get_settings", lambda: state.settings)
    return state


@pytest.fixture
def admin(member, test_db):
    member.role = "admin"
    test_db.commit()
    return member


def save(client, patient, items, expected=0, **extra):
    resp = client.put(
        url(patient), json={"expected_version": expected, "items": items, "avoid": [], **extra}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_current_list_carries_the_ledd_breakdown(auth_client, papa):
    save(auth_client, papa, FAMILY_STYLE)
    body = auth_client.get(url(papa)).json()
    result = body["ledd"]
    assert result["total_mg"] == 385.0
    rows = {c["substance"]: c for c in result["contributions"]}
    assert {k: rows[k]["mg"] for k in rows} == {
        "levodopa": 300.0,
        "rotigotina": 60.0,
        "pramipexol": 25.0,
    }
    ids = {i["name"]: i["id"] for g in body["slots"] for i in g["items"]}
    assert ids["Parmital"] in rows["pramipexol"]["item_ids"]
    assert len(rows["levodopa"]["item_ids"]) == 3
    assert all(c["source"] and c["factor"] for c in result["contributions"])


def test_no_dopaminergic_item_means_no_number(auth_client, papa):
    save(auth_client, papa, [med("Medicina Común", strength="50 mg")])
    result = auth_client.get(url(papa)).json()["ledd"]
    assert result["has_dopaminergic"] is False
    assert result["total_mg"] is None


def test_dry_run_and_save_show_the_change(auth_client, papa):
    first = save(auth_client, papa, FAMILY_STYLE)
    assert first["ledd_change"] == {"before_mg": None, "after_mg": 385.0, "pct": None}
    items = [
        {"id": i["id"], **{k: i[k] for k in ("name", "slot", "quantity", "strength", "dose_note")}}
        for g in first["slots"]
        for i in g["items"]
    ]
    items.append(med("Prolopa", "cena", strength="125 mg"))  # +100
    dry = save(auth_client, papa, items, expected=1, dry_run=True)
    assert dry["saved"] is False
    assert dry["ledd_change"] == {"before_mg": 385.0, "after_mg": 485.0, "pct": 26}
    assert dry["ledd"]["total_mg"] == 385.0  # nothing saved yet
    real = save(auth_client, papa, items, expected=1)
    assert real["saved"] is True
    assert real["ledd"]["total_mg"] == 485.0

    versions = auth_client.get(url(papa, "/versions")).json()
    assert [(v["version_no"], v["ledd_mg"]) for v in versions] == [(2, 485.0), (1, 385.0)]
    detail = auth_client.get(url(papa, "/versions/2")).json()
    assert detail["ledd"]["total_mg"] == 485.0
    assert detail["ledd_change"] == {"before_mg": 385.0, "after_mg": 485.0, "pct": 26}
    one = auth_client.get(url(papa, "/versions/1")).json()
    assert one["ledd_change"]["before_mg"] is None


def test_unchanged_ledd_has_no_change(auth_client, papa):
    first = save(auth_client, papa, FAMILY_STYLE)
    items = [
        {"id": i["id"], **{k: i[k] for k in ("name", "slot", "quantity", "strength", "dose_note")}}
        for g in first["slots"]
        for i in g["items"]
    ]
    items.append(med("Otra Medicina Común", "cena"))
    assert save(auth_client, papa, items, expected=1, dry_run=True)["ledd_change"] is None


def test_printed_sheet_header_shows_the_ledd(auth_client, papa):
    save(auth_client, papa, FAMILY_STYLE)
    resp = auth_client.get(url(papa, "/print.pdf"))
    assert resp.status_code == 200
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(resp.content)).pages)
    assert "LEDD ≈ 385 mg/día" in " ".join(text.split())


def _tool(db, name):
    run = tools.run_tool(tools.ToolContext(db=db), "toolu_1", name, {"patient": "papa"})
    assert run.ok
    return json.loads(run.block["content"])


def test_chat_tool_returns_the_breakdown(auth_client, papa, test_db):
    save(auth_client, papa, FAMILY_STYLE)
    data = _tool(test_db, "get_current_meds")
    assert data["ledd"]["total_mg_per_day"] == 385.0
    assert {c["substance"] for c in data["ledd"]["contributions"]} == {
        "Levodopa",
        "Rotigotina (parche)",
        "Pramipexol",
    }
    hist = _tool(test_db, "get_med_history")
    assert hist["list_versions"][0]["ledd_mg"] == 385.0


# ── Unknown names: AI proposal, admin review ─────────────────────────────────


def test_save_queues_each_unknown_name_once(auth_client, papa, test_db):
    save(auth_client, papa, FAMILY_STYLE)
    rows = test_db.scalars(select(LeddSubstance)).all()
    assert [(r.name_key, r.status) for r in rows] == [("medicina comun", "queued")]
    assert (
        test_db.scalar(
            select(func.count()).select_from(Job).where(Job.kind == ledd_substances.JOB_KIND)
        )
        == 1
    )
    # A dry run never queues; another save of a known name queues nothing.
    body = auth_client.get(url(papa)).json()
    items = [
        {"id": i["id"], "name": i["name"], "slot": i["slot"], "quantity": i["quantity"]}
        for g in body["slots"]
        for i in g["items"]
    ]
    save(auth_client, papa, [*items, med("Otra Más", "cena")], expected=1, dry_run=True)
    assert test_db.scalar(select(func.count()).select_from(LeddSubstance)) == 1
    save(auth_client, papa, [*items, med("MEDICINA común", "noche")], expected=1)
    assert test_db.scalar(select(func.count()).select_from(LeddSubstance)) == 1


def test_job_sends_only_the_name_and_strength(auth_client, papa, test_db, fake, caplog):
    save(auth_client, papa, [med("Sustancia Nueva", strength="10 mg")])
    with caplog.at_level(logging.INFO):
        assert jobs.run_next(fake.factory)
    [call] = fake.messages.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    [message] = call["messages"]
    assert message["content"] == [
        {"type": "text", "text": "Medicamento: Sustancia Nueva\nConcentración: 10 mg"}
    ]
    sent = json.dumps(call["messages"], ensure_ascii=False)
    assert "Papá" not in sent and "papa" not in sent.lower()
    assert "Sustancia Nueva" not in caplog.text  # logs: ids and counts only
    test_db.expire_all()
    row = test_db.scalars(select(LeddSubstance)).one()
    assert (row.status, row.is_dopaminergic, row.factor, row.confidence) == (
        "proposed",
        True,
        5.0,
        "medium",
    )
    # Never sent twice: a second job for the same row does not call.
    jobs.enqueue(test_db, ledd_substances.JOB_KIND, {"substance_id": row.id})
    test_db.commit()
    assert jobs.run_next(fake.factory)
    assert len(fake.messages.calls) == 1

    # The proposal is pending and never counts.
    result = auth_client.get(url(papa)).json()["ledd"]
    assert result["total_mg"] is None
    assert [p["name"] for p in result["pending"]] == ["Sustancia Nueva"]


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda m: setattr(m, "output", "not json"), "invalid_json"),
        (lambda m: setattr(m, "stop_reason", "refusal"), "refusal"),
        (lambda m: setattr(m, "output", {"is_dopaminergic": "yes"}), "invalid_json"),
    ],
)
def test_unusable_answer_is_recorded_failed(auth_client, papa, test_db, fake, setup, code):
    setup(fake.messages)
    save(auth_client, papa, [med("Sustancia Nueva", strength="10 mg")])
    jobs.run_next(fake.factory)
    test_db.expire_all()
    row = test_db.scalars(select(LeddSubstance)).one()
    assert (row.status, row.error) == ("failed", code)


def test_no_key_leaves_the_row_queued(auth_client, papa, test_db, fake):
    fake.settings = Settings(anthropic_api_key="")
    save(auth_client, papa, [med("Sustancia Nueva", strength="10 mg")])
    jobs.run_next(fake.factory)
    test_db.expire_all()
    assert test_db.scalars(select(LeddSubstance)).one().status == "queued"
    assert fake.messages.calls == []
    job = test_db.scalars(select(Job).where(Job.kind == ledd_substances.JOB_KIND)).one()
    assert job.status == "failed"


def _proposed(auth_client, papa, test_db, fake):
    items = [
        med("Levodopa", strength="100 mg"),
        med("Sustancia Nueva", "cena", strength="10 mg", quantity="½"),
    ]
    save(auth_client, papa, items)
    jobs.run_next(fake.factory)
    test_db.expire_all()
    return test_db.scalars(select(LeddSubstance)).one()


def test_review_endpoints_are_admin_only(auth_client, papa, test_db, fake):
    row = _proposed(auth_client, papa, test_db, fake)
    assert auth_client.get("/api/v1/ledd/substances").status_code == 403
    assert auth_client.post(f"/api/v1/ledd/substances/{row.id}/confirm", json={}).status_code == 403
    assert auth_client.post(f"/api/v1/ledd/substances/{row.id}/reject").status_code == 403


def test_admin_confirms_then_it_counts(auth_client, papa, test_db, fake, admin):
    row = _proposed(auth_client, papa, test_db, fake)
    listed = auth_client.get("/api/v1/ledd/substances?review=true").json()
    assert [(s["id"], s["needs_review"]) for s in listed] == [(row.id, True)]
    resp = auth_client.post(f"/api/v1/ledd/substances/{row.id}/confirm", json={"factor": 4})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    result = auth_client.get(url(papa)).json()["ledd"]
    assert result["pending"] == []
    assert result["total_mg"] == 100 + 5 * 4  # ½ × 10 mg × 4
    audit_row = test_db.scalars(select(AuditLog).where(AuditLog.entity == "ledd_substance")).one()
    assert (audit_row.action, audit_row.entity_id, audit_row.user_id) == (
        "approve",
        str(row.id),
        admin.id,
    )
    # Decided once.
    again = auth_client.post(f"/api/v1/ledd/substances/{row.id}/reject")
    assert again.status_code == 409


def test_admin_rejects_then_it_adds_zero(auth_client, papa, test_db, fake, admin):
    row = _proposed(auth_client, papa, test_db, fake)
    resp = auth_client.post(f"/api/v1/ledd/substances/{row.id}/reject")
    assert resp.status_code == 200
    result = auth_client.get(url(papa)).json()["ledd"]
    assert (result["total_mg"], result["pending"]) == (100.0, [])
    assert (
        test_db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "reject")
        )
        == 1
    )


def test_reference_table_is_public_to_members(auth_client):
    body = auth_client.get("/api/v1/ledd/reference").json()
    assert body["table_version"]
    assert all(s["source"] for s in body["substances"])


def test_worker_loads_the_handler():
    from app.worker import HANDLER_MODULES, load_handlers

    assert "app.services.ledd_substances" in HANDLER_MODULES
    load_handlers(HANDLER_MODULES)
    assert ledd_substances.JOB_KIND in jobs.HANDLERS


def test_request_timeout_fits_the_lock():
    worst = ledd_substances.REQUEST_TIMEOUT_SECONDS * (1 + ledd_substances.MAX_RETRIES)
    assert worst < jobs.LOCK_TIMEOUT.total_seconds() - 60
