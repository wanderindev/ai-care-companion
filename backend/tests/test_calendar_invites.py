"""Email calendar invites: the per-user setting, queueing on every write path
(not the backfill), and the worker job (METHOD / SEQUENCE / UID / STATUS,
MIME structure, one message per recipient, retries, missing key).

Synthetic data only. No real email is ever sent: ``send_message`` is faked
and the test environment logs instead of sending.
"""

import logging
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.user import User
from app.services import calendar_invites as invites
from app.services import email as email_svc
from app.services import jobs
from app.services import log_entries as entries_svc
from app.services.appointments_backfill import backfill
from app.services.whatsapp.chat import PANAMA_TZ

APPTS = "/api/v1/appointments"
ENTRIES = "/api/v1/log-entries"
SETTINGS = "/api/v1/me/settings"


def _future(days=30, hour=10) -> datetime:
    d = datetime.now(PANAMA_TZ).date() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=PANAMA_TZ)


def _past(days=30) -> datetime:
    return _future(-days)


@pytest.fixture
def factory(test_db, test_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture
def doctor(test_db):
    row = Doctor(name="Dra. Ana Pérez", aliases=[])
    test_db.add(row)
    test_db.commit()
    return row


@pytest.fixture
def opted_in(test_db, member):
    """``member`` (Ana) and Beto opted in; Carla inactive and Dani opted out."""
    member.email_invites = True
    test_db.add_all(
        [
            User(email="beto@example.com", display_name="Beto", email_invites=True),
            User(
                email="carla@example.com",
                display_name="Carla",
                email_invites=True,
                is_active=False,
            ),
            User(email="dani@example.com", display_name="Dani"),
        ]
    )
    test_db.commit()
    return member


@pytest.fixture
def sent(monkeypatch):
    """Production settings with a key; ``send_message`` records instead of sending."""
    settings = get_settings().model_copy(
        update={
            "environment": "production",
            "resend_api_key": "re_test_key",
            "app_base_url": "https://app.example.com",
            "email_from": "n16chat <acceso@example.com>",
        }
    )
    monkeypatch.setattr(invites, "get_settings", lambda: settings)
    messages = []
    monkeypatch.setattr(email_svc, "send_message", lambda _s, msg: messages.append(msg))
    return messages


def _invite_jobs(test_db) -> list[Job]:
    test_db.expire_all()
    return list(test_db.scalars(select(Job).where(Job.kind == invites.KIND).order_by(Job.id)))


def _run_all(test_db, factory) -> None:
    """Make every queued invite job due and run them."""
    test_db.execute(
        update(Job)
        .where(Job.kind == invites.KIND, Job.status == "queued")
        .values(run_after=func.now() - timedelta(seconds=1))
    )
    test_db.commit()
    while jobs.run_next(factory):
        pass
    test_db.expire_all()


def _create(client, **overrides) -> dict:
    body = {"patient_id": 1, "starts_at": _future().isoformat(), "reason": "Control"}
    body.update(overrides)
    resp = client.post(APPTS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _calendar_part(msg):
    parts = [p for p in msg.walk() if p.get_content_type() == "text/calendar"]
    assert len(parts) == 1
    return parts[0]


def _ics_value(ics: str, name: str) -> str:
    unfolded = ics.replace("\r\n ", "")
    for line in unfolded.split("\r\n"):
        head, _, value = line.partition(":")
        if head.split(";")[0] == name:
            return value
    raise AssertionError(f"{name} not in ICS")


# ── Setting ──────────────────────────────────────────────────────────────────


def test_settings_require_sign_in(db_client):
    assert db_client.get(SETTINGS).status_code == 401
    headers = {"X-Requested-With": "fetch"}
    resp = db_client.patch(SETTINGS, json={"email_invites": True}, headers=headers)
    assert resp.status_code == 401


def test_settings_patch_needs_csrf_header(auth_client):
    del auth_client.headers["X-Requested-With"]
    assert auth_client.patch(SETTINGS, json={"email_invites": True}).status_code == 403


def test_settings_get_patch_and_audit(auth_client, test_db, member):
    resp = auth_client.get(SETTINGS)
    assert resp.status_code == 200
    assert resp.json() == {"email": "ana@example.com", "email_invites": False}

    resp = auth_client.patch(SETTINGS, json={"email_invites": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_invites"] is True
    test_db.expire_all()
    assert test_db.get(User, member.id).email_invites is True
    # Unchanged value: no second audit row.
    auth_client.patch(SETTINGS, json={"email_invites": True})
    rows = test_db.scalars(select(AuditLog).where(AuditLog.entity == "user")).all()
    assert len(rows) == 1
    assert (rows[0].action, rows[0].user_id, rows[0].entity_id) == (
        "update",
        member.id,
        str(member.id),
    )
    assert (rows[0].before, rows[0].after) == ({"email_invites": False}, {"email_invites": True})

    assert auth_client.patch(SETTINGS, json={"email_invites": "x"}).status_code == 422
    assert auth_client.patch(SETTINGS, json={"role": "admin"}).status_code == 422


# ── Queueing ─────────────────────────────────────────────────────────────────


def test_api_create_update_cancel_delete_queue_one_job_each(auth_client, test_db, patients):
    appt = _create(auth_client)
    [job] = _invite_jobs(test_db)
    assert job.payload == {"appointment_id": appt["id"], "new": True}
    assert job.max_attempts == invites.MAX_ATTEMPTS
    # Debounced: due a minute later, not now.
    assert job.run_after > test_db.scalar(select(func.now()))

    # Further edits while it is still queued reuse it.
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"location": "Consultorio 2"})
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"status": "cancelada"})
    assert len(_invite_jobs(test_db)) == 1

    # Once that job has run, a new change gets a new job.
    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"status": "programada"})
    jobs_now = _invite_jobs(test_db)
    assert len(jobs_now) == 2
    assert jobs_now[-1].payload == {"appointment_id": appt["id"]}

    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    assert auth_client.delete(f"{APPTS}/{appt['id']}").status_code == 204
    assert len(_invite_jobs(test_db)) == 3


def test_notes_only_and_noop_changes_queue_nothing(auth_client, test_db, patients):
    appt = _create(auth_client)
    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"notes": "Nota sintética"})
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"reason": "Control"})
    assert [j.status for j in _invite_jobs(test_db)] == ["done"]


def test_edits_of_cancelled_appointment_queue_nothing(auth_client, test_db, patients):
    appt = _create(auth_client)
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"status": "cancelada"})
    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"location": "Otro lugar"})
    assert auth_client.delete(f"{APPTS}/{appt['id']}").status_code == 204
    assert len(_invite_jobs(test_db)) == 1


def test_delete_before_first_invite_drops_the_job(auth_client, test_db, patients):
    appt = _create(auth_client)
    assert auth_client.delete(f"{APPTS}/{appt['id']}").status_code == 204
    assert _invite_jobs(test_db) == []


def test_complete_sends_nothing(auth_client, test_db, patients):
    appt = _create(auth_client, starts_at=_past().isoformat())
    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    resp = auth_client.post(
        f"{APPTS}/{appt['id']}/complete",
        json={
            "patient_id": 1,
            "entry_date": _past().date().isoformat(),
            "entry_type": "consulta_control",
            "title": "Control",
        },
    )
    assert resp.status_code == 201, resp.text
    assert [j.status for j in _invite_jobs(test_db)] == ["done"]


def test_complete_with_next_appointment_queues_it(auth_client, test_db, patients):
    appt = _create(auth_client, starts_at=_past().isoformat())
    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    resp = auth_client.post(
        f"{APPTS}/{appt['id']}/complete",
        json={
            "patient_id": 1,
            "entry_date": _past().date().isoformat(),
            "entry_type": "consulta_control",
            "title": "Control",
            "next_appointment": {"date": _future().date().isoformat()},
        },
    )
    assert resp.status_code == 201, resp.text
    queued = [j for j in _invite_jobs(test_db) if j.status == "queued"]
    assert len(queued) == 1
    assert queued[0].payload["appointment_id"] != appt["id"]
    assert queued[0].payload["new"] is True


def test_bitacora_sync_queues_create_update_and_cancel(auth_client, test_db, patients):
    entry = {
        "patient_id": 1,
        "entry_date": datetime.now(PANAMA_TZ).date().isoformat(),
        "entry_type": "consulta_control",
        "title": "Control general",
        "next_appointment": {"date": _future().date().isoformat(), "time": "09:00"},
    }
    resp = auth_client.post(ENTRIES, json=entry)
    assert resp.status_code == 201, resp.text
    entry_id = resp.json()["id"]
    [job] = _invite_jobs(test_db)
    assert job.payload["new"] is True

    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    moved = {"date": _future(31).date().isoformat(), "time": "09:00"}
    resp = auth_client.patch(f"{ENTRIES}/{entry_id}", json={"next_appointment": moved})
    assert resp.status_code == 200, resp.text
    assert len(_invite_jobs(test_db)) == 2

    test_db.execute(update(Job).values(status="done"))
    test_db.commit()
    resp = auth_client.patch(f"{ENTRIES}/{entry_id}", json={"next_appointment": None})
    assert resp.status_code == 200, resp.text
    assert len(_invite_jobs(test_db)) == 3
    appt = test_db.scalar(select(Appointment))
    assert appt.status == "cancelada"


def test_backfill_queues_no_invites(test_db, patients, member):
    future = _future().date()
    entries_svc.create_entry(
        test_db,
        {
            "patient_id": patients[0].id,
            "entry_date": date.today(),
            "entry_type": "consulta_control",
            "title": "Control sintético",
            "review_status": "pending_review",
        },
        user_id=None,
        origin="import_docx",
        origin_ref={
            "source_sha256": "0" * 64,
            "entry_index": 1,
            "text_sha256": "1",
            "next_appointment": {"date": future.isoformat(), "time": None, "doctor": None},
        },
    )
    test_db.commit()
    report = backfill(test_db, user_id=member.id)
    test_db.commit()
    assert report.created == 1
    assert _invite_jobs(test_db) == []


# ── The job ──────────────────────────────────────────────────────────────────


def test_request_one_message_per_opted_in_active_user(
    auth_client, test_db, factory, patients, doctor, opted_in, sent
):
    appt = _create(auth_client, doctor_id=doctor.id, location="Piso 3", notes="Nota privada")
    _run_all(test_db, factory)

    assert [j.status for j in _invite_jobs(test_db)] == ["done"]
    assert sorted(m["To"] for m in sent) == ["ana@example.com", "beto@example.com"]
    msg = next(m for m in sent if m["To"] == "beto@example.com")
    assert msg["From"] == "n16chat <acceso@example.com>"
    starts = _future()
    months = invites.MONTHS
    assert msg["Subject"] == (
        f"Cita: Papá — Control con Dra. Ana Pérez — {starts.day} {months[starts.month - 1]} 10:00"
    )

    # multipart/mixed → [multipart/alternative → plain, html, calendar] + invite.ics
    assert msg.get_content_type() == "multipart/mixed"
    alt, attachment = msg.get_payload()
    assert alt.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in alt.get_payload()] == [
        "text/plain",
        "text/html",
        "text/calendar",
    ]
    assert attachment.get_content_type() == "application/ics"
    assert attachment.get_filename() == "invite.ics"

    cal = _calendar_part(msg)
    assert cal.get_param("method") == "REQUEST"
    assert cal.get_content_charset() == "utf-8"
    ics = cal.get_content()
    assert ics == attachment.get_content().decode()
    assert "\r\n" in ics and "\n" not in ics.replace("\r\n", "")
    assert _ics_value(ics, "METHOD") == "REQUEST"
    assert _ics_value(ics, "UID") == f"appointment-{appt['id']}@app.example.com"
    assert _ics_value(ics, "STATUS") == "CONFIRMED"
    row = test_db.get(Appointment, appt["id"])
    assert int(_ics_value(ics, "SEQUENCE")) == int(
        (row.updated_at - row.created_at).total_seconds()
    )
    unfolded = ics.replace("\r\n ", "")
    assert 'ORGANIZER;CN="n16chat":mailto:acceso@example.com' in unfolded
    assert (
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE;"
        'CN="Beto":mailto:beto@example.com'
    ) in unfolded
    assert "ana@example.com" not in unfolded  # each recipient sees only themselves
    assert "BEGIN:VTIMEZONE" in ics
    assert "DTSTART;TZID=America/Panama:" in ics

    # Notes never leave the app.
    for m in sent:
        assert "Nota privada" not in m.as_string()
    plain = alt.get_payload()[0].get_content()
    assert "Lugar: Piso 3" in plain
    assert "https://app.example.com/calendario?cita=" in plain


def test_update_then_cancel_and_delete_grow_sequence(
    auth_client, test_db, factory, patients, opted_in, sent
):
    appt = _create(auth_client)
    _run_all(test_db, factory)
    first = int(_ics_value(_calendar_part(sent[0]).get_content(), "SEQUENCE"))

    # Make the next change land later than the first invite.
    test_db.execute(
        update(Appointment).values(
            created_at=Appointment.created_at - timedelta(minutes=5),
        )
    )
    test_db.commit()
    sent.clear()
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"reason": "Control anual"})
    _run_all(test_db, factory)
    assert sent[0]["Subject"].startswith("Cita actualizada: Papá — Control anual — ")
    second = int(_ics_value(_calendar_part(sent[0]).get_content(), "SEQUENCE"))
    assert second > first

    sent.clear()
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"status": "cancelada"})
    _run_all(test_db, factory)
    cal = _calendar_part(sent[0])
    ics = cal.get_content()
    assert cal.get_param("method") == "CANCEL"
    assert _ics_value(ics, "METHOD") == "CANCEL"
    assert _ics_value(ics, "STATUS") == "CANCELLED"
    assert sent[0]["Subject"].startswith("Cita cancelada: Papá")
    third = int(_ics_value(ics, "SEQUENCE"))
    assert third >= second

    sent.clear()
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"status": "programada"})
    _run_all(test_db, factory)
    fourth = int(_ics_value(_calendar_part(sent[0]).get_content(), "SEQUENCE"))
    assert _calendar_part(sent[0]).get_param("method") == "REQUEST"

    sent.clear()
    assert auth_client.delete(f"{APPTS}/{appt['id']}").status_code == 204
    _run_all(test_db, factory)
    assert len(sent) == 2
    cal = _calendar_part(sent[0])
    ics = cal.get_content()
    assert cal.get_param("method") == "CANCEL"
    assert _ics_value(ics, "UID") == f"appointment-{appt['id']}@app.example.com"
    assert _ics_value(ics, "STATUS") == "CANCELLED"
    assert int(_ics_value(ics, "SEQUENCE")) == fourth + 1
    assert sent[0]["Subject"].startswith("Cita cancelada: Papá — Control anual — ")


def test_new_then_cancelled_before_running_sends_nothing(
    auth_client, test_db, factory, patients, opted_in, sent
):
    appt = _create(auth_client)
    auth_client.patch(f"{APPTS}/{appt['id']}", json={"status": "cancelada"})
    _run_all(test_db, factory)
    assert sent == []
    assert [j.status for j in _invite_jobs(test_db)] == ["done"]


def test_past_and_done_appointments_send_nothing(
    auth_client, test_db, factory, patients, opted_in, sent
):
    _create(auth_client, starts_at=_past().isoformat())
    _run_all(test_db, factory)
    assert sent == []

    appt = _create(auth_client)
    test_db.execute(
        update(Appointment).where(Appointment.id == appt["id"]).values(status="realizada")
    )
    test_db.commit()
    _run_all(test_db, factory)
    assert sent == []


def test_all_day_invite_uses_dates(auth_client, test_db, factory, patients, opted_in, sent):
    day = _future().date()
    _create(auth_client, starts_at=_future(hour=0).isoformat(), all_day=True)
    _run_all(test_db, factory)
    ics = _calendar_part(sent[0]).get_content()
    assert f"DTSTART;VALUE=DATE:{day:%Y%m%d}" in ics
    assert sent[0]["Subject"].endswith(f"{day.day} {invites.MONTHS[day.month - 1]}")


def test_no_recipients_sends_nothing(auth_client, test_db, factory, patients, sent):
    _create(auth_client)
    _run_all(test_db, factory)
    assert sent == []
    assert [j.status for j in _invite_jobs(test_db)] == ["done"]


def test_smtp_error_is_retried_without_leaking(
    auth_client, test_db, factory, patients, opted_in, sent, monkeypatch, caplog
):
    def boom(_settings, msg):
        raise OSError(f"smtp down for {msg['To']}")

    monkeypatch.setattr(email_svc, "send_message", boom)
    _create(auth_client)
    with caplog.at_level(logging.INFO):
        _run_all(test_db, factory)
    [job] = _invite_jobs(test_db)
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.error == f"JobError: {invites.SEND_FAILED}"
    assert "example.com" not in caplog.text
    assert "Control" not in caplog.text


def test_missing_key_in_production_skips_with_warning(
    auth_client, test_db, factory, patients, opted_in, sent, monkeypatch, caplog
):
    settings = invites.get_settings().model_copy(update={"resend_api_key": ""})
    monkeypatch.setattr(invites, "get_settings", lambda: settings)
    appt = _create(auth_client)
    with caplog.at_level(logging.WARNING, logger="app.calendar_invites"):
        _run_all(test_db, factory)
    assert sent == []
    assert [j.status for j in _invite_jobs(test_db)] == ["done"]
    assert f"appointment {appt['id']} not sent" in caplog.text


def test_dev_mode_logs_instead_of_sending(
    auth_client, test_db, factory, patients, opted_in, monkeypatch, caplog
):
    messages = []
    monkeypatch.setattr(email_svc, "send_message", lambda _s, msg: messages.append(msg))
    appt = _create(auth_client)
    with caplog.at_level(logging.INFO, logger="app.calendar_invites"):
        _run_all(test_db, factory)
    assert messages == []
    assert (
        f"DEV calendar invite (REQUEST) for appointment {appt['id']} to 2 recipient(s)"
        in caplog.text
    )
    assert "example.com" not in caplog.text
    assert "Control" not in caplog.text


def test_worker_loads_the_handler():
    from app.worker import HANDLER_MODULES

    assert "app.services.calendar_invites" in HANDLER_MODULES
    assert invites.KIND in jobs.HANDLERS


def test_param_and_subject_are_sanitized():
    assert invites._param('Ana "la" \n Pérez') == '"Ana la Pérez"'
