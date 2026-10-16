"""Backfill analysis runs (ticket #40): message selection by date range over
every import, the backfill prompt and run instructions, patient focus,
cross-check, past appointments and old medication changes out of scope,
no day analyzed twice (and no rejected suggestion re-proposed), the cost
guard, stop / resume, waiting for media, continuation jobs, the estimate,
undo (reject pending), the admin API and log privacy.

Synthetic data only: invented people, doctors, medicines and messages. The
Anthropic API is never called: the analysis runs against a fake client.
"""

import logging
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.main import app
from app.models.analysis_run import AnalysisRun
from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.suggestion import Suggestion
from app.prompts import (
    backfill_analysis_v1,
    backfill_analysis_v2,
    daily_analysis_v1,
    daily_analysis_v2,
)
from app.services import jobs, log_entries, meds, suggestions
from app.services.appointments import today_local
from app.services.attachments import JPEG
from app.services.daily_analysis import analyze, backfill, pipeline
from tests.test_daily_analysis import RATIONALE, SECRET_TEXT, FakeMessages, _at, _import, _msg

URL = "/api/v1/analysis-runs"
DAY = date(2026, 3, 2)


@pytest.fixture
def env(monkeypatch, test_engine):
    state = SimpleNamespace(
        settings=Settings(
            anthropic_api_key="test-key",
            analysis_model="claude-opus-5-5",
            analysis_max_days_per_job=14,
            backfill_max_cost_usd=20,
        ),
        fake=FakeMessages(),
        factory=sessionmaker(bind=test_engine, autoflush=False),
    )
    monkeypatch.setattr(
        analyze,
        "make_client",
        lambda key: SimpleNamespace(beta=SimpleNamespace(messages=state.fake)),
    )
    monkeypatch.setattr(backfill, "get_settings", lambda: state.settings)
    app.dependency_overrides[get_settings] = lambda: state.settings
    yield state
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def admin(auth_client, member, test_db):
    member.role = "admin"
    member.can_import = True
    test_db.commit()
    return auth_client


@pytest.fixture
def papa(patients):
    return patients[0]


@pytest.fixture
def mama(patients):
    return patients[1]


@pytest.fixture
def doctor(test_db):
    row = Doctor(name="Dr. Bruno Ficticio", specialty="Neurología")
    test_db.add(row)
    test_db.commit()
    return row


def _run_all(env) -> None:
    while jobs.run_next(env.factory):
        pass


def _start(client, patient, **kw):
    body = {
        "patient_id": patient.id,
        "date_from": DAY.isoformat(),
        "date_to": (DAY + timedelta(days=6)).isoformat(),
        **kw,
    }
    resp = client.post(URL, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run(db, run_id) -> AnalysisRun:
    db.expire_all()
    return db.get(AnalysisRun, run_id)


def _suggestions(db) -> list[Suggestion]:
    db.expire_all()
    return list(db.scalars(select(Suggestion).order_by(Suggestion.id)))


def _entry(m, patient="mama", day=DAY, **kw):
    return {
        "kind": "create_entry",
        "patient": patient,
        "entry_date": day.isoformat(),
        "entry_time": "",
        "doctor_id": 0,
        "source_label": "",
        "entry_type": "consulta_control",
        "channel": "presencial",
        "title": "Control de neurología",
        "indications": "Seguir igual.",
        "family_notes": "",
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
        **kw,
    }


def _appointment(m, when: date, patient="mama"):
    return {
        "kind": "create_appointment",
        "patient": patient,
        "date": when.isoformat(),
        "time": "10:00",
        "doctor_id": 0,
        "location": "",
        "reason": "Control",
        "notes": "",
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
    }


def _med_change(m, patient="mama"):
    blank = {
        k: ""
        for k in ("strength", "quantity", "dose_note", "schedule_note", "instructions", "reason")
    }
    return {
        "kind": "med_change",
        "patient": patient,
        "action": "add",
        "medication": "Medicina Nueva",
        "item_id": 0,
        "name": "Medicina Nueva",
        "slot": "desayuno",
        "prescriber_id": 0,
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
        **blank,
    }


# ── Prompt, settings, registration ───────────────────────────────────────────


def test_v2_prompt_adds_explicit_requests_and_keeps_the_rest():
    spec = backfill_analysis_v2.SPEC
    assert spec.version == "backfill_analysis_v2"
    assert spec.schema is daily_analysis_v1.SCHEMA
    assert daily_analysis_v2.EXPLICIT_RULES in spec.system
    assert "[PEDIDO EXPLÍCITO]" in spec.system and "VERIFICACIÓN" in spec.system
    assert backfill_analysis_v2.run_instructions is backfill_analysis_v1.run_instructions
    assert backfill_analysis_v1.SPEC.version == "backfill_analysis_v1"  # v1 untouched
    assert "PEDIDO EXPLÍCITO" not in backfill_analysis_v1.SYSTEM_PROMPT


def test_prompt_is_versioned_and_reuses_the_daily_schema():
    spec = backfill_analysis_v1.SPEC
    assert spec.version == "backfill_analysis_v1"
    assert spec.schema is daily_analysis_v1.SCHEMA
    assert spec.system != daily_analysis_v1.SYSTEM_PROMPT
    assert "VERIFICACIÓN" in spec.system and "Nunca inventes" in spec.system
    text = backfill_analysis_v1.run_instructions(
        patient_slug="mama",
        patient_name="Mamá",
        cross_check=True,
        today=date(2026, 10, 1),
        meds_since=date(2026, 6, 1),
    )
    assert text.startswith("INSTRUCCIONES DE ESTE ANÁLISIS")
    assert "Mamá (mama)" in text and "VERIFICACIÓN" in text
    assert "2026-10-01" in text and "2026-06-01" in text


def test_settings_defaults_and_handler_registered():
    s = Settings()
    assert s.backfill_max_cost_usd == 20.0
    assert s.backfill_max_range_days == 366
    from app.worker import HANDLER_MODULES, load_handlers

    load_handlers(HANDLER_MODULES)
    assert "app.services.daily_analysis.backfill" in HANDLER_MODULES
    assert backfill.JOB_KIND in jobs.HANDLERS


# ── Message selection ────────────────────────────────────────────────────────


def test_range_messages_cover_every_import_by_panama_day(test_db):
    first = _import(test_db, "labs_only")
    second = _import(test_db, "backfill")
    before = _msg(test_db, first, _at(DAY - timedelta(days=1), 23, 59), "Antes")
    a = _msg(test_db, first, _at(DAY, 0, 0), "Del primer import")
    b = _msg(test_db, second, _at(DAY + timedelta(days=1), 23, 59), "Del segundo import")
    after = _msg(test_db, second, _at(DAY + timedelta(days=2), 0, 0), "Después")
    got = pipeline.range_messages(test_db, DAY, DAY + timedelta(days=1))
    assert [m.id for m in got] == [a.id, b.id]
    assert before.id not in {m.id for m in got} and after.id not in {m.id for m in got}


# ── A run end to end ─────────────────────────────────────────────────────────


def test_run_builds_history_for_the_focus_patient(test_db, env, admin, papa, mama):
    meds.save_list(
        test_db,
        mama.id,
        items=[{"name": "Medicina Base", "slot": "desayuno"}],
        avoid=[],
        expected_version=0,
        user_id=None,
        effective_date=DAY + timedelta(days=1),
    )
    test_db.commit()
    imp = _import(test_db, "backfill", new=0)
    m1 = _msg(test_db, imp, _at(DAY, 9), "Mamá fue al neurólogo, todo bien")
    m2 = _msg(test_db, imp, _at(DAY + timedelta(days=2), 9), "Mamá empezó Medicina Nueva")
    future = today_local() + timedelta(days=30)
    past = DAY + timedelta(days=5)

    def answer(params):
        day_text = params["messages"][0]["content"][1]["text"]
        if DAY.isoformat() in day_text:
            return {
                "suggestions": [
                    _entry(m1),
                    _entry(m1, patient="papa", title="Otra cosa"),
                    _appointment(m1, past),
                    _appointment(m1, future),
                    _med_change(m1),  # before the list's «Desde»: out of scope
                ]
            }
        return {"suggestions": [_med_change(m2)]}  # after «Desde»: kept

    env.fake.default = answer
    run = _start(admin, mama)
    assert run["status"] == "queued" and run["stats"]["days_already_done"] == []
    _run_all(env)

    assert len(env.fake.calls) == 2
    params = env.fake.calls[0]
    assert params["system"][0]["text"] == backfill_analysis_v2.SYSTEM_PROMPT
    context, day = params["messages"][0]["content"]
    assert context["text"].startswith("INSTRUCCIONES DE ESTE ANÁLISIS")
    assert "Mamá (mama)" in context["text"]
    assert (DAY + timedelta(days=1)).isoformat() in context["text"]  # meds «Desde»
    assert "Medicina Base" in context["text"]  # today's list
    assert day["text"].startswith("MENSAJES del lunes 2026-03-02")

    rows = _suggestions(test_db)
    assert [(s.kind, s.patient_id) for s in rows] == [
        ("create_entry", mama.id),
        ("create_appointment", mama.id),
        ("med_change", mama.id),
    ]
    assert all(s.analysis_run_id == run["id"] and s.import_id is None for s in rows)
    assert rows[1].payload["starts_at"].startswith(future.isoformat())

    done = admin.get(f"{URL}/{run['id']}").json()
    assert done["status"] == "done" and done["finished_at"]
    assert done["pending_suggestions"] == 3
    stats = done["stats"]
    assert stats["prompt_version"] == "backfill_analysis_v2"
    assert stats["days_total"] == 2 and len(stats["days_done"]) == 2
    assert stats["suggestions_created"] == 3
    assert stats["skipped_out_of_scope"] == 3  # papa, past appointment, old med change
    assert stats["cost_usd"] > 0
    # The suggestion API shows the run.
    listed = admin.get("/api/v1/suggestions").json()["items"]
    assert {s["analysis_run_id"] for s in listed} == {run["id"]}


def test_cross_check_only_proposes_missing_events(test_db, env, admin, papa, doctor):
    existing = log_entries.create_entry(
        test_db,
        {
            "patient_id": papa.id,
            "entry_date": DAY + timedelta(days=1),  # the bitácora dates it a day later
            "entry_type": "consulta_control",
            "doctor_id": doctor.id,
            "title": "Control neurológico",
        },
        user_id=None,
    )
    test_db.commit()
    imp = _import(test_db, "backfill", new=0)
    m = _msg(test_db, imp, _at(DAY, 9), "Papá fue al Dr. Ficticio")
    env.fake.default = {
        "suggestions": [
            _entry(m, patient="papa", doctor_id=doctor.id),  # same visit, ±1 day: duplicate
            _entry(m, patient="papa", entry_type="examen", title="Resonancia"),  # missing
            {
                "kind": "update_entry",
                "patient": "papa",
                "entry_id": existing.id,
                "title": "",
                "add_indications": "Algo más",
                "add_family_notes": "",
                "rationale": RATIONALE,
                "source_message_ids": [m.id],
            },
            _med_change(m, patient="papa"),
        ]
    }
    run = _start(admin, papa, cross_check=True)
    _run_all(env)
    rows = _suggestions(test_db)
    assert [(s.kind, s.payload.get("title")) for s in rows] == [("create_entry", "Resonancia")]
    stats = _run(test_db, run["id"]).stats
    assert stats["skipped_duplicate"] == 1 and stats["skipped_out_of_scope"] == 2
    assert "VERIFICACIÓN" in env.fake.calls[0]["messages"][0]["content"][0]["text"]


def test_same_options_never_analyze_a_day_twice(test_db, env, admin, mama):
    imp = _import(test_db, "backfill", new=0)
    m = _msg(test_db, imp, _at(DAY, 9), "Mamá tuvo fiebre")
    env.fake.default = {
        "suggestions": [_entry(m, entry_type="observacion_familiar", title="Fiebre")]
    }
    first = _start(admin, mama)
    _run_all(env)
    assert len(env.fake.calls) == 1
    row = _suggestions(test_db)[0]
    suggestions.reject(test_db, row.id, user_id=None)
    test_db.commit()

    # Same patient and options over an overlapping range: the day is skipped.
    second = _start(admin, mama, date_from=(DAY - timedelta(days=3)).isoformat())
    assert second["stats"]["days_already_done"] == [DAY.isoformat()]
    _run_all(env)
    assert len(env.fake.calls) == 1
    assert _run(test_db, second["id"]).status == "done"

    # reanalyze asks explicitly: the day is read again, but the rejected
    # suggestion is not proposed again.
    third = _start(admin, mama, reanalyze=True)
    assert third["stats"]["days_already_done"] == []
    _run_all(env)
    assert len(env.fake.calls) == 2
    assert len(_suggestions(test_db)) == 1
    assert _run(test_db, third["id"]).stats["skipped_duplicate"] == 1
    assert first["id"] != second["id"] != third["id"]


def test_continuation_jobs_and_progress(test_db, env, admin, mama, monkeypatch):
    env.settings = Settings(anthropic_api_key="k", analysis_max_days_per_job=2)
    imp = _import(test_db, "backfill", new=0)
    for offset in range(3):
        _msg(test_db, imp, _at(DAY + timedelta(days=offset), 9), f"Mensaje del día {offset}")
    seen = []
    monkeypatch.setattr(
        jobs.JobContext, "progress", lambda self, pct, msg=None: seen.append((pct, msg))
    )
    run = _start(admin, mama)
    assert jobs.run_next(env.factory)
    assert len(env.fake.calls) == 2
    assert _run(test_db, run["id"]).status == "running"
    follow = test_db.scalars(
        select(Job).where(Job.kind == backfill.JOB_KIND, Job.status == "queued")
    ).one()
    assert follow.payload == {"run_id": run["id"], "waits": backfill.MAX_WAITS}
    assert _run(test_db, run["id"]).job_id == follow.id
    _run_all(env)
    assert len(env.fake.calls) == 3
    assert _run(test_db, run["id"]).status == "done"
    assert (0, "Analizando día 1 de 3") in seen and (66, "Analizando día 3 de 3") in seen


def test_waits_for_media_in_progress(test_db, env, admin, mama, storage):
    imp = _import(test_db, "backfill", new=0)
    photo = _msg(
        test_db,
        imp,
        _at(DAY, 9),
        media=(b"\xff\xd8\xff" + b"\x00" * 30, JPEG, "image"),
        storage=storage,
    )
    _msg(test_db, imp, _at(DAY, 9, 5), "Foto de la receta")
    busy = jobs.enqueue(test_db, "classify_media", {"attachment_id": photo.attachment_id})
    busy.status = "running"
    test_db.commit()
    run = _start(admin, mama)
    assert jobs.run_next(env.factory)
    assert env.fake.calls == []
    row = _run(test_db, run["id"])
    assert row.status == "waiting" and row.stats["waits"] == 1
    again = test_db.get(Job, row.job_id)
    assert again.payload == {"run_id": run["id"], "waits": 1} and again.status == "queued"


# ── Cost guard, stop, resume, failures ───────────────────────────────────────


def test_cost_limit_stops_and_resume_needs_a_higher_limit(test_db, env, admin, mama):
    imp = _import(test_db, "backfill", new=0)
    for offset in range(3):
        _msg(test_db, imp, _at(DAY + timedelta(days=offset), 9), f"Mensaje {offset}")
    run = _start(admin, mama, max_cost_usd=0.01)  # one fake call costs more
    _run_all(env)
    assert len(env.fake.calls) == 1
    stopped = admin.get(f"{URL}/{run['id']}").json()
    assert stopped["status"] == "stopped" and stopped["stop_reason"] == "cost_limit"
    assert len(stopped["stats"]["days_done"]) == 1

    resp = admin.post(f"{URL}/{run['id']}/resume", json={})
    assert resp.status_code == 422
    resp = admin.post(f"{URL}/{run['id']}/resume", json={"max_cost_usd": 5})
    assert resp.status_code == 200 and resp.json()["status"] == "queued"
    _run_all(env)
    assert len(env.fake.calls) == 3
    assert _run(test_db, run["id"]).status == "done"


def test_resume_replaces_a_queued_job(test_db, env, admin, mama):
    imp = _import(test_db, "backfill", new=0)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    run = _start(admin, mama)
    old_job = run["job_id"]
    assert admin.post(f"{URL}/{run['id']}/stop").status_code == 200
    resumed = admin.post(f"{URL}/{run['id']}/resume").json()
    assert resumed["job_id"] != old_job
    _run_all(env)
    assert len(env.fake.calls) == 1
    test_db.expire_all()
    assert test_db.get(Job, old_job).status == "done"
    assert _run(test_db, run["id"]).status == "done"


def test_stop_before_the_job_runs(test_db, env, admin, mama):
    imp = _import(test_db, "backfill", new=0)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    run = _start(admin, mama)
    resp = admin.post(f"{URL}/{run['id']}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped" and resp.json()["stop_reason"] == "manual"
    _run_all(env)
    assert env.fake.calls == []
    assert _run(test_db, run["id"]).status == "stopped"
    assert admin.post(f"{URL}/{run['id']}/stop").status_code == 409
    audit = test_db.scalars(select(AuditLog).where(AuditLog.entity == "analysis_run")).all()
    assert {a.action for a in audit} == {"create", "update"}


def test_missing_key_fails_and_can_be_resumed(test_db, env, admin, mama):
    env.settings = Settings(anthropic_api_key="")
    imp = _import(test_db, "backfill", new=0)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    run = _start(admin, mama)
    _run_all(env)
    assert admin.get(f"{URL}/{run['id']}").json()["status"] == "failed"
    # A failed run does not block a new one, and can be resumed once fixed.
    env.settings = Settings(anthropic_api_key="k")
    assert admin.post(f"{URL}/{run['id']}/resume").status_code == 200
    _run_all(env)
    assert _run(test_db, run["id"]).status == "done"
    assert len(env.fake.calls) == 1


# ── Undo, estimate, API rules ────────────────────────────────────────────────


def test_reject_pending_undoes_a_run(test_db, env, admin, mama):
    imp = _import(test_db, "backfill", new=0)
    m = _msg(test_db, imp, _at(DAY, 9), "Mamá fue al control")
    env.fake.default = {"suggestions": [_entry(m)]}
    run = _start(admin, mama)
    assert admin.post(f"{URL}/{run['id']}/reject-pending").status_code == 409  # still active
    _run_all(env)
    other = suggestions.create_suggestion(
        test_db,
        kind="create_entry",
        payload={
            "patient_id": mama.id,
            "entry_date": DAY.isoformat(),
            "entry_type": "otro",
            "title": "X",
        },
        rationale="Otra fuente",
    )
    test_db.commit()
    resp = admin.post(f"{URL}/{run['id']}/reject-pending")
    assert resp.status_code == 200 and resp.json() == {"rejected": 1}
    statuses = {s.id: s.status for s in _suggestions(test_db)}
    assert statuses[other.id] == "pending"
    assert sorted(statuses.values()) == ["pending", "rejected"]
    assert admin.get(f"{URL}/{run['id']}").json()["pending_suggestions"] == 0


def test_estimate_counts_without_calling_claude(test_db, env, admin, mama, storage):
    imp = _import(test_db, "backfill", new=0)
    m = _msg(test_db, imp, _at(DAY, 9), "Mamá fue al control")
    _msg(test_db, imp, _at(DAY + timedelta(days=1), 9), "👍")  # nothing to read
    _msg(
        test_db,
        imp,
        _at(DAY + timedelta(days=2), 9),
        media=(b"\xff\xd8\xff" + b"\x00" * 30, JPEG, "image"),
        storage=storage,
    )
    body = {
        "patient_id": mama.id,
        "date_from": DAY.isoformat(),
        "date_to": (DAY + timedelta(days=6)).isoformat(),
    }
    est = admin.post(f"{URL}/estimate", json=body).json()
    assert est["days_with_messages"] == 3 and est["days_to_analyze"] == 3
    assert est["days_with_content"] == 1 and est["calls"] == 1 and est["jobs"] == 1
    assert est["messages"] == 3 and est["media_unresolved"] == 1
    assert est["media_in_progress"] == 0
    assert 0 < est["cost_usd"] < 1 and est["max_cost_usd"] == 20
    assert env.fake.calls == []
    assert test_db.scalars(select(AnalysisRun)).all() == []

    env.fake.default = {"suggestions": [_entry(m)]}
    _start(admin, mama)
    _run_all(env)
    est = admin.post(f"{URL}/estimate", json=body).json()
    assert est["days_already_done"] == 3 and est["days_to_analyze"] == 0 and est["calls"] == 0
    est = admin.post(f"{URL}/estimate", json={**body, "reanalyze": True}).json()
    assert est["days_to_analyze"] == 3


def test_api_rules(test_db, env, admin, auth_client, mama, member):
    today = today_local()
    base = {"patient_id": mama.id, "date_from": DAY.isoformat(), "date_to": DAY.isoformat()}
    cases = [
        ({**base, "date_from": (DAY + timedelta(days=1)).isoformat()}, 422),
        ({**base, "date_to": (today + timedelta(days=1)).isoformat()}, 422),
        ({**base, "date_from": (DAY - timedelta(days=400)).isoformat()}, 422),
        ({**base, "patient_id": 999_999}, 404),
        ({**base, "max_cost_usd": 0}, 422),
    ]
    for body, code in cases:
        assert admin.post(URL, json=body).status_code == code, body
    assert admin.post(URL, json=base).status_code == 201
    resp = admin.post(URL, json=base)
    assert resp.status_code == 409 and resp.json()["detail"] == backfill.ALREADY_RUNNING
    assert len(admin.get(URL).json()) == 1
    assert admin.get(f"{URL}/999999").status_code == 404

    member.role = "member"
    test_db.commit()
    assert auth_client.get(URL).status_code == 403
    assert auth_client.post(f"{URL}/estimate", json=base).status_code == 403


def test_logs_carry_no_message_content(test_db, env, admin, mama, caplog):
    imp = _import(test_db, "backfill", new=0)
    m = _msg(test_db, imp, _at(DAY, 9), SECRET_TEXT)
    env.fake.default = {"suggestions": [_entry(m, title=SECRET_TEXT)]}
    _start(admin, mama)
    with caplog.at_level(logging.DEBUG):
        _run_all(env)
    assert "backfill analysis: run" in caplog.text
    assert SECRET_TEXT not in caplog.text and RATIONALE not in caplog.text
