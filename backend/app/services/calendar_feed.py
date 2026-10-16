"""Per-user ICS calendar feed (DESIGN §6.3): tokens and the RFC 5545 body.

Tokens mirror magic links and sessions (``app.services.auth``): 256 random
bits, URL-safe, and only their SHA-256 is stored, so a feed is looked up by
hash through a unique index (cheap; nothing to brute-force in 2^256). The
full URL is returned once, when the feed is generated.

What the feed carries (it lands in third-party calendars such as Google or
Zoho, so it carries only what a calendar needs):

- every appointment of **both** patients that starts at most
  ``WINDOW_PAST_DAYS`` ago, plus all future ones, in every status;
- ``SUMMARY``: «Papá: <reason or "Cita"> — <doctor>», prefixed with
  «Cancelada · » or «✓ » for cancelled and done appointments;
- ``LOCATION``, and a ``DESCRIPTION`` with the doctor, the reason, the place
  and a link back to the app. Free-text ``notes`` are **never** included;
- ``STATUS``: ``CANCELLED`` for ``cancelada``, ``CONFIRMED`` otherwise;
- stable ``UID`` ``appointment-<id>@app.example.com``, and a ``SEQUENCE`` that
  grows with every edit (seconds between ``created_at`` and ``updated_at``),
  so clients replace the event they already have;
- times in ``America/Panama`` (``TZID`` + a ``VTIMEZONE``: fixed UTC-5, no
  DST); an all-day appointment is a ``VALUE=DATE`` event ending the next day.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.appointment import STATUS_CANCELLED, STATUS_DONE, Appointment
from app.models.calendar_feed import CalendarFeed
from app.models.patient import Patient
from app.models.user import User
from app.services import audit
from app.services.appointments import to_local
from app.services.auth import hash_token, new_token

ENTITY = "calendar_feed"
WINDOW_PAST_DAYS = 183
TZID = "America/Panama"
UID_DOMAIN = "app.example.com"
CALENDAR_NAME = "n16chat — Citas"

# secrets.token_urlsafe(32) gives 43 characters; anything that does not look
# like a token is rejected before touching the database.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def feed_path(token: str) -> str:
    return f"/api/v1/calendar/{token}.ics"


def feed_url(base_url: str, token: str) -> str:
    return base_url.rstrip("/") + feed_path(token)


def webcal_url(url: str) -> str:
    """The ``webcal://`` form Apple Calendar opens as a subscription."""
    return re.sub(r"^https?://", "webcal://", url)


# ── Feed records ───────────────────────────────────────────────────────────


def active_feed(db: DbSession, user_id: int) -> CalendarFeed | None:
    return db.scalar(
        select(CalendarFeed).where(
            CalendarFeed.user_id == user_id, CalendarFeed.revoked_at.is_(None)
        )
    )


def _audit_view(feed: CalendarFeed) -> dict:
    # Never the token or its hash.
    return jsonable_encoder(
        {
            "user_id": feed.user_id,
            "created_at": feed.created_at,
            "revoked_at": feed.revoked_at,
        }
    )


def revoke(db: DbSession, user_id: int) -> CalendarFeed | None:
    """Revoke the user's active feed (audited); the caller commits."""
    feed = active_feed(db, user_id)
    if feed is None:
        return None
    before = _audit_view(feed)
    feed.revoked_at = utcnow()
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity=ENTITY,
        entity_id=feed.id,
        before=before,
        after=_audit_view(feed),
    )
    return feed


def regenerate(db: DbSession, user: User) -> tuple[CalendarFeed, str]:
    """Revoke the active feed (if any) and create a new one; returns
    ``(feed, token)``. The caller commits. The user row is locked so two
    concurrent requests cannot both create an active feed."""
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    revoke(db, user.id)
    token = new_token()
    feed = CalendarFeed(user_id=user.id, token_hash=hash_token(token))
    db.add(feed)
    db.flush()
    db.refresh(feed)
    audit.record(
        db,
        user_id=user.id,
        action=audit.ACTION_CREATE,
        entity=ENTITY,
        entity_id=feed.id,
        after=_audit_view(feed),
    )
    return feed, token


def feed_for_token(db: DbSession, token: str) -> CalendarFeed | None:
    """The active feed of an active user for ``token``, else None."""
    if not _TOKEN_RE.match(token):
        return None
    return db.scalar(
        select(CalendarFeed)
        .join(User, User.id == CalendarFeed.user_id)
        .where(
            CalendarFeed.token_hash == hash_token(token),
            CalendarFeed.revoked_at.is_(None),
            User.is_active.is_(True),
        )
    )


# ── ICS rendering ────────────────────────────────────────────────────────────


def escape_text(value: str) -> str:
    """RFC 5545 §3.3.11 TEXT escaping (backslash, ``;``, ``,``, newlines)."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold_line(line: str, limit: int = 75) -> str:
    """Fold a content line at ``limit`` octets (RFC 5545 §3.1), never inside
    a UTF-8 character. Continuation lines start with one space."""
    out: list[str] = []
    current = ""
    size = 0
    for char in line:
        n = len(char.encode())
        if size + n > limit:
            out.append(current)
            current = " "
            size = 1
        current += char
        size += n
    out.append(current)
    return "\r\n".join(out)


def _utc_stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _local_stamp(value: datetime) -> str:
    return to_local(value).strftime("%Y%m%dT%H%M%S")


def _date_stamp(value: date) -> str:
    return value.strftime("%Y%m%d")


@dataclass(frozen=True)
class FeedEvent:
    appointment: Appointment
    patient_name: str


def sequence(appt: Appointment) -> int:
    """Grows with every edit: whole seconds from creation to the last update."""
    return max(0, int((appt.updated_at - appt.created_at).total_seconds()))


def summary(appt: Appointment, patient_name: str) -> str:
    text = f"{patient_name}: {appt.reason or 'Cita'}"
    if appt.doctor is not None:
        text += f" — {appt.doctor.name}"
    if appt.status == STATUS_CANCELLED:
        return f"Cancelada · {text}"
    if appt.status == STATUS_DONE:
        return f"✓ {text}"
    return text


def description(appt: Appointment, patient_name: str, link: str) -> str:
    lines = [f"Paciente: {patient_name}"]
    if appt.doctor is not None:
        lines.append(f"Médico: {appt.doctor.name}")
    if appt.reason:
        lines.append(f"Motivo: {appt.reason}")
    if appt.location:
        lines.append(f"Lugar: {appt.location}")
    if appt.status == STATUS_CANCELLED:
        lines.append("Estado: cancelada")
    elif appt.status == STATUS_DONE:
        lines.append("Estado: realizada")
    lines.append(f"Ver en n16chat: {link}")
    return "\n".join(lines)


def uid(appointment_id: int) -> str:
    """The event's stable ``UID``, shared by the feed and the email invites."""
    return f"appointment-{appointment_id}@{UID_DOMAIN}"


def appointment_link(base_url: str, appointment_id: int) -> str:
    return f"{base_url.rstrip('/')}/calendario?cita={appointment_id}"


def vevent(
    appt: Appointment,
    patient_name: str,
    base_url: str,
    *,
    seq: int | None = None,
    dtstamp: datetime | None = None,
    cancelled: bool | None = None,
    extra: Iterable[str] = (),
) -> list[str]:
    """One VEVENT's content lines (unfolded). The feed uses the defaults; the
    email invites (``calendar_invites``) pass their own ``SEQUENCE``, the
    message's ``DTSTAMP``, ``cancelled`` for a ``METHOD:CANCEL`` and the
    ``ORGANIZER`` / ``ATTENDEE`` lines in ``extra``."""
    link = appointment_link(base_url, appt.id)
    if cancelled is None:
        cancelled = appt.status == STATUS_CANCELLED
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid(appt.id)}",
        f"DTSTAMP:{_utc_stamp(dtstamp or appt.updated_at)}",
        f"CREATED:{_utc_stamp(appt.created_at)}",
        f"LAST-MODIFIED:{_utc_stamp(appt.updated_at)}",
        f"SEQUENCE:{sequence(appt) if seq is None else seq}",
    ]
    if appt.all_day:
        day = to_local(appt.starts_at).date()
        lines += [
            f"DTSTART;VALUE=DATE:{_date_stamp(day)}",
            f"DTEND;VALUE=DATE:{_date_stamp(day + timedelta(days=1))}",
            "TRANSP:TRANSPARENT",
        ]
    else:
        ends_at = appt.starts_at + timedelta(minutes=appt.duration_min)
        lines += [
            f"DTSTART;TZID={TZID}:{_local_stamp(appt.starts_at)}",
            f"DTEND;TZID={TZID}:{_local_stamp(ends_at)}",
            "TRANSP:OPAQUE",
        ]
    lines.append(f"SUMMARY:{escape_text(summary(appt, patient_name))}")
    if appt.location:
        lines.append(f"LOCATION:{escape_text(appt.location)}")
    lines += [
        f"DESCRIPTION:{escape_text(description(appt, patient_name, link))}",
        f"URL:{link}",
        *extra,
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        "END:VEVENT",
    ]
    return lines


# Panama: UTC-5 all year, no daylight saving time.
VTIMEZONE = (
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:-0500",
    "TZOFFSETTO:-0500",
    "TZNAME:EST",
    "END:STANDARD",
    "END:VTIMEZONE",
)


def serialize(lines: Iterable[str]) -> str:
    """Content lines → an ICS body: each folded at 75 octets, CRLF-terminated."""
    return "".join(fold_line(line) + "\r\n" for line in lines)


def render_calendar(events: Iterable[FeedEvent], base_url: str) -> str:
    """The whole VCALENDAR, CRLF line endings, folded at 75 octets."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//n16chat//Citas//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(CALENDAR_NAME)}",
        f"X-WR-TIMEZONE:{TZID}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        *VTIMEZONE,
    ]
    for event in events:
        lines += vevent(event.appointment, event.patient_name, base_url)
    lines.append("END:VCALENDAR")
    return serialize(lines)


def feed_events(db: DbSession, *, now: datetime | None = None) -> list[FeedEvent]:
    """Appointments in the feed window (both patients, every status)."""
    since = (now or utcnow()) - timedelta(days=WINDOW_PAST_DAYS)
    names = dict(db.execute(select(Patient.id, Patient.display_name)).all())
    appts = db.scalars(
        select(Appointment)
        .where(Appointment.starts_at >= since)
        .order_by(Appointment.starts_at, Appointment.id)
    ).all()
    return [FeedEvent(a, names.get(a.patient_id, "Paciente")) for a in appts]
