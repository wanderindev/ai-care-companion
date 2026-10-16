"""ICS calendar feed: tokens, management API, feed content and access.

Synthetic data only: invented doctors, places and reasons.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.core.logging import RedactSecretsFilter
from app.models.appointment import Appointment
from app.models.audit import AuditLog
from app.models.calendar_feed import CalendarFeed
from app.models.doctor import Doctor
from app.services import calendar_feed as svc

FEED = "/api/v1/calendar/feed"
APPTS = "/api/v1/appointments"


def _path(url: str) -> str:
    return url[url.index("/api/v1/") :]


def _generate(client) -> dict:
    resp = client.post(FEED)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _unfold(body: str) -> list[str]:
    """A tiny RFC 5545 reader: checks CRLF and line length, unfolds."""
    assert body.endswith("\r\n")
    raw = body[:-2].split("\r\n")
    assert all("\n" not in line and "\r" not in line for line in raw)
    assert all(len(line.encode()) <= 75 for line in raw), "unfolded line over 75 octets"
    lines: list[str] = []
    for line in raw:
        if line.startswith(" "):
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _events(body: str) -> list[dict[str, str]]:
    events, current = [], None
    for line in _unfold(body):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None:
            name, _, value = line.partition(":")
            current[name] = value
    return events


@pytest.fixture
def doctor(test_db):
    row = Doctor(name="Dra. Ana Pérez", aliases=[])
    test_db.add(row)
    test_db.commit()
    return row


@pytest.fixture
def feed_client(auth_client, patients, doctor):
    return auth_client


def _create_appt(client, **overrides):
    body = {"patient_id": 1, "starts_at": "2030-11-03T14:30:00-05:00", "reason": "Control"}
    body.update(overrides)
    resp = client.post(APPTS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Management API ───────────────────────────────────────────────────────────


def test_management_requires_sign_in(db_client):
    headers = {"X-Requested-With": "fetch"}
    assert db_client.get(FEED).status_code == 401
    assert db_client.post(FEED, headers=headers).status_code == 401
    assert db_client.delete(FEED, headers=headers).status_code == 401


def test_management_requires_csrf_header(auth_client):
    del auth_client.headers["X-Requested-With"]
    assert auth_client.post(FEED).status_code == 403


def test_generate_stores_only_hash_and_audits_without_token(auth_client, test_db, member):
    assert auth_client.get(FEED).json() == {"active": False, "created_at": None}

    data = _generate(auth_client)
    token = data["url"].rsplit("/", 1)[1].removesuffix(".ics")
    assert data["url"] == f"http://localhost:5180/api/v1/calendar/{token}.ics"
    assert data["webcal_url"] == f"webcal://localhost:5180/api/v1/calendar/{token}.ics"
    assert len(token) >= 43

    feed = test_db.scalar(select(CalendarFeed))
    assert feed.user_id == member.id
    assert feed.token_hash == svc.hash_token(token)
    # The raw token is nowhere in the database.
    dump = test_db.execute(text("SELECT row_to_json(c)::text FROM calendar_feeds c")).scalar()
    audits = test_db.execute(text("SELECT before::text, after::text FROM audit_log")).all()
    assert token not in dump
    for before, after in audits:
        for blob in (before or "", after or ""):
            assert token not in blob
            assert feed.token_hash not in blob
    assert test_db.scalar(select(AuditLog.entity)) == "calendar_feed"

    status = auth_client.get(FEED).json()
    assert status["active"] is True and status["created_at"]
    assert "url" not in status


def test_regenerate_revokes_old_token(feed_client, test_db):
    old = _path(_generate(feed_client)["url"])
    new = _path(_generate(feed_client)["url"])
    assert old != new

    feeds = test_db.scalars(select(CalendarFeed).order_by(CalendarFeed.id)).all()
    assert [f.revoked_at is not None for f in feeds] == [True, False]
    feed_client.cookies.clear()
    assert feed_client.get(old).status_code == 404
    assert feed_client.get(new).status_code == 200
    actions = test_db.scalars(select(AuditLog.action).order_by(AuditLog.id)).all()
    assert actions == ["create", "update", "create"]


def test_delete_revokes(feed_client):
    path = _path(_generate(feed_client)["url"])
    assert feed_client.delete(FEED).status_code == 204
    assert feed_client.get(FEED).json()["active"] is False
    assert feed_client.get(path).status_code == 404
    # Idempotent.
    assert feed_client.delete(FEED).status_code == 204


# ── Feed access ──────────────────────────────────────────────────────────────


def test_feed_without_cookie(feed_client):
    path = _path(_generate(feed_client)["url"])
    feed_client.cookies.clear()
    resp = feed_client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/calendar; charset=utf-8"
    assert resp.headers["cache-control"] == "private, max-age=900"
    assert resp.text.startswith("BEGIN:VCALENDAR\r\n")

    head = feed_client.head(path)
    assert head.status_code == 200
    assert head.headers["etag"] == resp.headers["etag"]

    cached = feed_client.get(path, headers={"If-None-Match": resp.headers["etag"]})
    assert cached.status_code == 304


@pytest.mark.parametrize(
    "token",
    ["unknown-token-0123456789abcdefghijklmnopqrstuv", "short", "a%20b" * 10],
)
def test_unknown_token_404(db_client, token):
    resp = db_client.get(f"/api/v1/calendar/{token}.ics")
    assert resp.status_code == 404
    assert token not in resp.text


def test_inactive_user_404(feed_client, test_db, member):
    path = _path(_generate(feed_client)["url"])
    member.is_active = False
    test_db.commit()
    feed_client.cookies.clear()
    assert feed_client.get(path).status_code == 404


def test_feed_path_is_redacted_from_access_log(feed_client):
    path = _path(_generate(feed_client)["url"])
    token = path.rsplit("/", 1)[1].removesuffix(".ics")
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET", path, "1.1", 200),
        None,
    )
    RedactSecretsFilter().filter(record)
    assert token not in record.getMessage()


# ── Feed content ─────────────────────────────────────────────────────────────


def _feed_body(client) -> str:
    path = _path(_generate(client)["url"])
    resp = client.get(path)
    assert resp.status_code == 200
    return resp.text


def test_calendar_structure(feed_client):
    lines = _unfold(_feed_body(feed_client))
    for expected in (
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:n16chat — Citas",
        "X-WR-TIMEZONE:America/Panama",
        "BEGIN:VTIMEZONE",
        "TZID:America/Panama",
        "TZOFFSETTO:-0500",
    ):
        assert expected in lines
    assert any(line.startswith("PRODID:") for line in lines)
    assert lines[-1] == "END:VCALENDAR"


def test_event_fields(feed_client, doctor):
    timed = _create_appt(
        feed_client,
        doctor_id=doctor.id,
        location="Clínica Uno, piso 3; consultorio 4",
        notes="Nota privada sintética",
        duration_min=45,
    )
    all_day = _create_appt(
        feed_client, patient_id=2, starts_at="2030-12-01T00:00:00-05:00", all_day=True
    )
    cancelled = _create_appt(feed_client, starts_at="2030-12-05T09:00:00-05:00")
    feed_client.patch(f"{APPTS}/{cancelled['id']}", json={"status": "cancelada"})

    body = _feed_body(feed_client)
    assert "Nota privada" not in body
    events = {e["UID"]: e for e in _events(body)}
    assert set(events) == {
        f"appointment-{a['id']}@app.example.com" for a in (timed, all_day, cancelled)
    }

    e = events[f"appointment-{timed['id']}@app.example.com"]
    assert e["SUMMARY"] == "Papá: Control — Dra. Ana Pérez"
    assert e["DTSTART;TZID=America/Panama"] == "20301103T143000"
    assert e["DTEND;TZID=America/Panama"] == "20301103T151500"
    assert e["LOCATION"] == "Clínica Uno\\, piso 3\\; consultorio 4"
    assert "Médico: Dra. Ana Pérez\\n" in e["DESCRIPTION"]
    assert e["URL"] == f"http://localhost:5180/calendario?cita={timed['id']}"
    assert e["STATUS"] == "CONFIRMED"
    assert e["DTSTAMP"].endswith("Z")

    e = events[f"appointment-{all_day['id']}@app.example.com"]
    assert e["SUMMARY"] == "Mamá: Control"
    assert e["DTSTART;VALUE=DATE"] == "20301201"
    assert e["DTEND;VALUE=DATE"] == "20301202"

    e = events[f"appointment-{cancelled['id']}@app.example.com"]
    assert e["STATUS"] == "CANCELLED"
    assert e["SUMMARY"].startswith("Cancelada · Papá")


def test_window_keeps_recent_past_and_future(feed_client, test_db):
    now = datetime.now(UTC)
    old = _create_appt(feed_client, starts_at=(now - timedelta(days=400)).isoformat())
    recent = _create_appt(feed_client, starts_at=(now - timedelta(days=30)).isoformat())
    future = _create_appt(feed_client, starts_at=(now + timedelta(days=900)).isoformat())
    uids = {e["UID"] for e in _events(_feed_body(feed_client))}
    assert f"appointment-{old['id']}@app.example.com" not in uids
    assert f"appointment-{recent['id']}@app.example.com" in uids
    assert f"appointment-{future['id']}@app.example.com" in uids


def test_sequence_grows_on_edit(feed_client, test_db):
    appt = _create_appt(feed_client)
    path = _path(_generate(feed_client)["url"])
    first = _events(feed_client.get(path).text)[0]
    assert first["SEQUENCE"] == "0"

    feed_client.patch(f"{APPTS}/{appt['id']}", json={"reason": "Control anual"})
    # Edits in the same second would not move the counter; age the row.
    row = test_db.get(Appointment, appt["id"])
    test_db.refresh(row)
    row.created_at = row.created_at - timedelta(minutes=5)
    test_db.commit()
    second = _events(feed_client.get(path).text)[0]
    assert int(second["SEQUENCE"]) >= 300
    assert second["UID"] == first["UID"]
    assert second["SUMMARY"] == "Papá: Control anual"


# ── Unit: escaping and folding ──────────────────────────────────────────────


def test_escape_text():
    assert svc.escape_text("a,b;c\\d\ne\r\nf") == "a\\,b\\;c\\\\d\\ne\\nf"


def test_fold_line_is_utf8_safe():
    line = "SUMMARY:" + "ñ" * 100
    folded = svc.fold_line(line)
    parts = folded.split("\r\n")
    assert len(parts) > 1
    assert all(len(p.encode()) <= 75 for p in parts)
    assert all(p.startswith(" ") for p in parts[1:])
    assert "".join([parts[0]] + [p[1:] for p in parts[1:]]) == line
