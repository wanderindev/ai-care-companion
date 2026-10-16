"""AI inference of the missing doctor of bitácora entries (#125).
Synthetic data and a fake client only."""

import json
import re
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.main import app
from app.models.appointment import Appointment
from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.log_entry import LogEntry
from app.models.suggestion import Suggestion
from app.services import doctor_inference as svc
from app.services import jobs
from app.services import suggestions as sugg
from app.services.appointments import PANAMA_TZ

URL = "/api/v1/doctor-inference"
SETTINGS = Settings(anthropic_api_key="test-key", analysis_model="claude-opus-5-5")


class FakeClient:
    """``beta.messages.create`` answers from ``decide(entry_id) -> dict | None``."""

    def __init__(self, decide, stop_reason="end_turn"):
        self.decide = decide
        self.stop_reason = stop_reason
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **params):
        self.calls.append(params)
        batch = params["messages"][0]["content"][1]["text"]
        ids = [int(i) for i in re.findall(r"\[entry (\d+)\]", batch)]
        items = []
        for entry_id in ids:
            answer = self.decide(entry_id)
            if answer is not None:
                items.append({"entry_id": entry_id, "reason": "Pista sintética", **answer})
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=json.dumps({"entries": items}))],
        )


def _entry(db, patient, day, entry_type="consulta_control", doctor=None, title="Control"):
    entry = LogEntry(
        patient_id=patient.id,
        entry_date=day,
        entry_type=entry_type,
        doctor_id=doctor.id if doctor else None,
        title=title,
        indications="Gotas oftálmicas sintéticas cada 8 horas",
    )
    db.add(entry)
    db.flush()
    return entry


@pytest.fixture
def world(test_db, patients):
    papa, mama = patients
    papa.full_name = "Nombre Completo Sintético"
    papa.id_number = "0-000-0000"
    oft = Doctor(name="Dra. Oftalmo Sintética", specialty="Oftalmología", aliases=["Dra. Ojo"])
    neuro = Doctor(name="Dr. Neuro Sintético", specialty="Neurología")
    retired = Doctor(name="Dr. Inactivo", specialty="Oftalmología", is_active=False)
    test_db.add_all([oft, neuro, retired])
    test_db.flush()
    w = SimpleNamespace(papa=papa, mama=mama, oft=oft, neuro=neuro, retired=retired)
    w.consulta = _entry(test_db, papa, date(2025, 3, 10), title="Control de la vista")
    w.examen = _entry(test_db, papa, date(2025, 3, 12), "examen", title="EEG")
    w.terapia = _entry(test_db, papa, date(2025, 3, 14), "terapia", title="Terapia")
    w.observacion = _entry(test_db, papa, date(2025, 3, 11), "observacion_familiar")
    w.hospital = _entry(test_db, papa, date(2025, 3, 11), "hospitalizacion")
    w.with_doctor = _entry(test_db, papa, date(2025, 2, 1), doctor=oft, title="Control previo")
    w.far = _entry(test_db, papa, date(2024, 12, 1), doctor=neuro, title="Muy lejano")
    w.mama_entry = _entry(test_db, mama, date(2025, 3, 10))
    w.appt = Appointment(
        patient_id=papa.id,
        doctor_id=neuro.id,
        starts_at=datetime(2025, 3, 20, 9, 0, tzinfo=PANAMA_TZ),
        notes="Nota privada sintética",
    )
    w.cancelled = Appointment(
        patient_id=papa.id,
        doctor_id=oft.id,
        starts_at=datetime(2025, 3, 21, 9, 0, tzinfo=PANAMA_TZ),
        status="cancelada",
    )
    test_db.add_all([w.appt, w.cancelled])
    test_db.commit()
    return w


def decider(w, overrides=None):
    table = {
        w.consulta.id: {"doctor_id": w.oft.id, "confidence": "high"},
        w.examen.id: {"doctor_id": w.neuro.id, "confidence": "medium"},
        w.terapia.id: {"doctor_id": 0, "confidence": "low"},
    }
    table.update(overrides or {})
    return lambda entry_id: table.get(entry_id)


def _suggestions(db):
    db.expire_all()
    return list(db.scalars(select(Suggestion).order_by(Suggestion.id)))


# ── candidates and context ──────────────────────────────────────────────────


def test_candidates_types_and_range(test_db, world):
    ids = [e.id for e in svc.candidates(test_db, world.papa.id)]
    assert ids == [world.consulta.id, world.examen.id, world.terapia.id]
    ranged = svc.candidates(test_db, world.papa.id, date(2025, 3, 11), date(2025, 3, 13))
    assert [e.id for e in ranged] == [world.examen.id]


def test_candidates_skip_already_proposed(test_db, world):
    sugg.create_suggestion(
        test_db,
        kind="update_entry",
        payload={"entry_id": world.consulta.id, "changes": {"doctor_id": world.oft.id}},
        rationale="x",
        source="ai",
    )
    rejected = sugg.create_suggestion(
        test_db,
        kind="update_entry",
        payload={"entry_id": world.examen.id, "changes": {"doctor_id": world.neuro.id}},
        rationale="x",
        source="ai",
    )
    test_db.flush()
    sugg.reject(test_db, rejected.id, user_id=None)
    # A pending WhatsApp proposal of a doctor also counts; other changes do not.
    sugg.create_suggestion(
        test_db,
        kind="update_entry",
        payload={"entry_id": world.terapia.id, "changes": {"title": "Otro título"}},
        rationale=None,
    )
    test_db.commit()
    assert [e.id for e in svc.candidates(test_db, world.papa.id)] == [world.terapia.id]
    sugg.create_suggestion(
        test_db,
        kind="update_entry",
        payload={"entry_id": world.terapia.id, "changes": {"doctor_id": world.neuro.id}},
        rationale=None,
    )
    test_db.commit()
    assert svc.candidates(test_db, world.papa.id) == []


def test_context_window_and_privacy(test_db, world):
    text = svc.batch_text(test_db, world.papa.id, [world.consulta])
    assert f"[entry {world.consulta.id}]" in text
    assert "Control previo" in text  # 37 days before: in the window
    assert "Muy lejano" not in text  # 99 days before
    assert f"2025-03-20 · {world.neuro.id} · Neurología" in text
    assert "2025-03-21" not in text  # cancelled
    assert "Nota privada" not in text
    assert "[entry" not in text.split("ENTRADAS CERCANAS")[1]
    doctors = svc.doctors_text(svc.active_doctors(test_db).values())
    assert "Dra. Ojo" in doctors and "Oftalmología" in doctors
    assert "Inactivo" not in doctors
    for secret in ("Nombre Completo", "0-000-0000", "Papá"):
        assert secret not in text and secret not in doctors


# ── the run ─────────────────────────────────────────────────────────────────


def test_only_high_confidence_with_active_doctor_becomes_suggestion(test_db, world):
    client = FakeClient(decider(world))
    stats = svc.run_inference(test_db, SETTINGS, world.papa.id, client=client, user_id=None)
    assert (stats.candidates, stats.batches, stats.suggestions, stats.unsure) == (3, 1, 1, 1)
    params = client.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["output_config"]["format"]["schema"] == svc.SPEC.schema
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    [row] = _suggestions(test_db)
    assert (row.kind, row.source, row.status, row.patient_id) == (
        "update_entry",
        "ai",
        "pending",
        world.papa.id,
    )
    assert row.payload == {"entry_id": world.consulta.id, "changes": {"doctor_id": world.oft.id}}
    assert row.rationale == "Pista sintética"
    assert row.source_message_ids == []

    again = FakeClient(decider(world))
    stats = svc.run_inference(test_db, SETTINGS, world.papa.id, client=again)
    # the proposed entry is never sent again; the other two are
    assert stats.candidates == 2
    sent = again.calls[0]["messages"][0]["content"][1]["text"]
    assert f"[entry {world.consulta.id}]" not in sent


@pytest.mark.parametrize("case", ["zero", "unknown", "inactive", "medium"])
def test_unusable_answers_create_nothing(test_db, world, case):
    doctor_id = {
        "zero": 0,
        "unknown": 999999,
        "inactive": world.retired.id,
        "medium": world.oft.id,
    }[case]
    confidence = "medium" if case == "medium" else "high"
    decide = decider(world, {world.consulta.id: {"doctor_id": doctor_id, "confidence": confidence}})
    svc.run_inference(test_db, SETTINGS, world.papa.id, client=FakeClient(decide))
    assert _suggestions(test_db) == []


def test_bad_answers(test_db, world):
    stats = svc.run_inference(
        test_db, SETTINGS, world.papa.id, client=FakeClient(decider(world), "max_tokens")
    )
    assert stats.errors == {"truncated": 1}
    assert _suggestions(test_db) == []
    assert svc.parse_answers({"nope": []}, {1}) is None
    parsed = svc.parse_answers(
        {
            "entries": [
                {"entry_id": 1, "doctor_id": 5, "confidence": "high", "reason": "a"},
                {"entry_id": 1, "doctor_id": 6, "confidence": "high", "reason": "b"},
                {"entry_id": 2, "doctor_id": 5, "confidence": "high", "reason": "c"},
                {"entry_id": 3, "doctor_id": 5, "confidence": "sure", "reason": "d"},
            ]
        },
        {1, 3},
    )
    assert list(parsed) == [1] and parsed[1].doctor_id == 5


def test_entry_that_got_a_doctor_meanwhile_is_skipped(test_db, world, monkeypatch):
    client = FakeClient(decider(world))
    answer = client.create

    def create(**params):
        test_db.execute(
            LogEntry.__table__.update()
            .where(LogEntry.id == world.consulta.id)
            .values(doctor_id=world.neuro.id)
        )
        test_db.commit()
        return answer(**params)

    client.beta.messages.create = create
    stats = svc.run_inference(test_db, SETTINGS, world.papa.id, client=client)
    assert (stats.suggestions, stats.skipped) == (0, 1)


def test_missing_key_and_bad_params(test_db, world):
    with pytest.raises(jobs.PermanentJobError):
        svc.run_inference(test_db, Settings(anthropic_api_key=""), world.papa.id)
    with pytest.raises(svc.InferenceError):
        svc.estimate(test_db, SETTINGS, world.papa.id, date(2025, 4, 1), date(2025, 3, 1))


def test_request_worst_case_fits_job_lock():
    backoff = 10 * svc.MAX_RETRIES  # the SDK's backoff is at most 8 s a retry
    worst = svc.REQUEST_TIMEOUT_SECONDS * (1 + svc.MAX_RETRIES) + backoff
    assert worst < jobs.LOCK_TIMEOUT.total_seconds() - 60


def test_handler_module_registered():
    from app.worker import HANDLER_MODULES, load_handlers

    assert "app.services.doctor_inference" in HANDLER_MODULES
    load_handlers(HANDLER_MODULES)
    assert svc.JOB_KIND in jobs.HANDLERS


# ── suggestions: creation guards and approval ───────────────────────────────


def test_ai_source_only_fills_a_missing_doctor(test_db, world):
    def create(entry, changes):
        return sugg.create_suggestion(
            test_db,
            kind="update_entry",
            payload={"entry_id": entry.id, "changes": changes},
            rationale="x",
            source="ai",
        )

    with pytest.raises(sugg.SuggestionError):
        create(world.consulta, {"doctor_id": world.oft.id, "title": "Otro"})
    with pytest.raises(sugg.SuggestionError):
        create(world.consulta, {"doctor_id": world.retired.id})
    with pytest.raises(sugg.SuggestionError) as exc:
        create(world.with_doctor, {"doctor_id": world.neuro.id})
    assert exc.value.status == 409


def test_approve_sets_doctor_and_is_audited(auth_client, test_db, world, member):
    svc.run_inference(test_db, SETTINGS, world.papa.id, client=FakeClient(decider(world)))
    [row] = _suggestions(test_db)
    resp = auth_client.post(f"/api/v1/suggestions/{row.id}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    test_db.expire_all()
    assert test_db.get(LogEntry, world.consulta.id).doctor_id == world.oft.id
    audit_row = test_db.scalar(
        select(AuditLog).where(
            AuditLog.entity == "log_entry",
            AuditLog.entity_id == str(world.consulta.id),
            AuditLog.action == "update",
        )
    )
    assert audit_row.user_id == member.id
    assert audit_row.before["doctor_id"] is None
    assert audit_row.after["doctor_id"] == world.oft.id


def test_approve_refused_once_entry_has_a_doctor(auth_client, test_db, world):
    svc.run_inference(test_db, SETTINGS, world.papa.id, client=FakeClient(decider(world)))
    [row] = _suggestions(test_db)
    entry = test_db.get(LogEntry, world.consulta.id)
    entry.doctor_id = world.neuro.id
    test_db.commit()
    resp = auth_client.post(f"/api/v1/suggestions/{row.id}/approve")
    assert resp.status_code == 409
    assert "ya tiene médico" in resp.json()["detail"]
    test_db.expire_all()
    assert test_db.get(LogEntry, world.consulta.id).doctor_id == world.neuro.id
    assert test_db.get(Suggestion, row.id).status == "pending"


# ── API and job ─────────────────────────────────────────────────────────────


@pytest.fixture
def admin_client(auth_client, member, test_db):
    member.role = "admin"
    test_db.commit()
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    return auth_client


def test_admin_only(auth_client, world):
    body = {"patient_id": world.papa.id}
    assert auth_client.post(f"{URL}/estimate", json=body).status_code == 403
    assert auth_client.post(URL, json=body).status_code == 403


def test_estimate_start_and_job(admin_client, test_db, test_engine, world, monkeypatch):
    body = {"patient_id": world.papa.id, "date_from": "2025-01-01", "date_to": "2025-12-31"}
    est = admin_client.post(f"{URL}/estimate", json=body).json()
    assert (est["candidates"], est["batches"], est["running_job_id"]) == (3, 1, None)
    assert est["model"] == "claude-opus-5-5" and est["cost_usd"] > 0

    bad = {**body, "date_from": "2026-01-01"}
    assert admin_client.post(f"{URL}/estimate", json=bad).status_code == 422
    assert admin_client.post(URL, json={"patient_id": 999999}).status_code == 404

    resp = admin_client.post(URL, json=body)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert admin_client.post(URL, json=body).status_code == 409  # one at a time
    assert admin_client.post(f"{URL}/estimate", json=body).json()["running_job_id"] == job_id
    job = test_db.get(Job, job_id)
    assert set(job.payload) == {"patient_id", "date_from", "date_to", "user_id"}

    monkeypatch.setattr(svc, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(svc, "make_client", lambda key: FakeClient(decider(world)))
    factory = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    assert jobs.run_next(factory) is True
    test_db.refresh(job)
    assert (job.status, job.progress, job.message) == ("done", 100, "Listo: 1 sugerencia de médico")
    [row] = _suggestions(test_db)
    assert row.created_by is not None


def test_start_without_key_or_candidates(admin_client, test_db, world):
    app.dependency_overrides[get_settings] = lambda: Settings(anthropic_api_key="")
    assert admin_client.post(URL, json={"patient_id": world.papa.id}).status_code == 503
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    body = {"patient_id": world.mama.id, "date_from": "2026-01-01"}
    assert admin_client.post(URL, json=body).status_code == 409
