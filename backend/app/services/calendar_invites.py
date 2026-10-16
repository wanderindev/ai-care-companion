"""Email calendar invites (DESIGN §6.3): «Recibir invitaciones por correo».

Users who opt in (``users.email_invites``) get an email with an iTIP
invitation (RFC 5546) whenever an appointment is created, meaningfully changed,
cancelled or deleted, so it lands in their calendar at once instead of waiting
for the ICS feed's hours-long refresh.

Queueing (API side, in the caller's transaction, through the hooks
``on_create`` / ``on_update`` / ``on_delete`` that ``app.services.appointments``
calls; ``notify=False`` there skips them, which the operator-run backfill uses
so ~50 historic appointments never email anybody):

- create of a ``programada`` appointment; an update of any of
  ``MEANINGFUL_FIELDS`` that leaves it ``programada`` (incl. a cancelled or
  done one set back to ``programada``) or moves it to ``cancelada``; a hard
  delete of a ``programada`` one. ``notes`` alone, edits of a cancelled or
  done appointment, and «Marcar como realizada» send nothing.
- one ``appointment_invite`` job per appointment, payload ids only
  (``{"appointment_id": …, "new": true?}``), due ``DEBOUNCE`` later. A
  further change while it is still queued reuses it and pushes it back
  (debounce + coalesce), so a burst of edits sends one email. ``new`` marks a
  job for an appointment nobody was invited to yet: if it is cancelled or
  deleted before the job runs, nothing is sent (a delete drops the job).

The job (worker side) reloads the appointment and decides then:

- ``programada`` → ``METHOD:REQUEST``, ``STATUS:CONFIRMED``; ``cancelada`` →
  ``METHOD:CANCEL``, ``STATUS:CANCELLED``; ``realizada`` → nothing;
- deleted → ``METHOD:CANCEL`` built from the delete's audit snapshot (the
  audit row keeps the full appointment), ``SEQUENCE`` + 1;
- nothing for an appointment that has already ended (past ones are history,
  not something to put in a calendar), nor for a ``new`` one cancelled
  before it was ever announced.

``UID`` and ``SEQUENCE`` are the feed's (``calendar_feed.uid`` /
``sequence``: seconds from ``created_at`` to ``updated_at``). Every change
that queues a job moves ``updated_at`` forward, and the debounce keeps a
sent invite at least ``DEBOUNCE`` older than the next change, so each invite
for a UID carries a strictly greater ``SEQUENCE`` than the one before.

One message per recipient (addresses never shared), active opted-in users
only: ``multipart/mixed`` → [``multipart/alternative`` → text/plain,
text/html, ``text/calendar; method=…``] + ``invite.ics`` attachment (the
structure Google Calendar itself sends; Gmail, Outlook and Apple Mail show
it as an invitation). ``ORGANIZER`` is ``EMAIL_FROM``; ``ATTENDEE`` is the
recipient with ``PARTSTAT=NEEDS-ACTION;RSVP=FALSE`` (calendar insertion, no
replies expected). The invite carries patient display name, doctor, reason,
place and time — never the free-text ``notes``.

Delivery: local environments log a one-line notice instead of sending (like
magic links); production without ``RESEND_API_KEY`` logs a warning and sends
nothing (the job finishes). An SMTP error raises ``JobError`` (retried with
backoff); a retry re-sends to every recipient, which is harmless: the same
UID and SEQUENCE just update the same event. Logs carry the appointment id,
method and recipient count only.
"""

import html
import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.models.appointment import STATUS_CANCELLED, STATUS_DONE, STATUS_SCHEDULED, Appointment
from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.patient import Patient
from app.models.user import User
from app.services import email as email_svc
from app.services import jobs
from app.services.appointments import local_datetime, to_local
from app.services.calendar_feed import VTIMEZONE, appointment_link, sequence, serialize, vevent

logger = logging.getLogger("app.calendar_invites")

KIND = "appointment_invite"
DEBOUNCE = timedelta(seconds=60)
MAX_ATTEMPTS = 5
METHOD_REQUEST = "REQUEST"
METHOD_CANCEL = "CANCEL"
SEND_FAILED = "No se pudo enviar la invitación por correo."

# A change to any of these is worth an email; ``notes`` (never sent) and the
# bitácora links are not.
MEANINGFUL_FIELDS = (
    "patient_id",
    "doctor_id",
    "starts_at",
    "duration_min",
    "all_day",
    "location",
    "reason",
    "status",
)

MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
MONTHS_LONG = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def utcnow() -> datetime:
    return datetime.now(UTC)


# ── Queueing (called by app.services.appointments) ─────────────────────────


def _pending(db: DbSession, appointment_id: int) -> Job | None:
    """The appointment's invite job that has not started yet, locked."""
    return db.scalar(
        select(Job)
        .where(
            Job.kind == KIND,
            Job.status == "queued",
            Job.payload["appointment_id"].as_integer() == appointment_id,
        )
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def queue(db: DbSession, appointment_id: int, *, new: bool = False) -> Job:
    """Queue (or push back the already queued) invite job for the appointment."""
    job = _pending(db, appointment_id)
    if job is None:
        payload: dict[str, Any] = {"appointment_id": appointment_id}
        if new:
            payload["new"] = True
        job = jobs.enqueue(db, KIND, payload, max_attempts=MAX_ATTEMPTS)
    job.run_after = func.now() + DEBOUNCE
    db.flush()
    return job


def on_create(db: DbSession, appt: Appointment) -> None:
    if appt.status == STATUS_SCHEDULED:
        queue(db, appt.id, new=True)


def on_update(
    db: DbSession, appt: Appointment, before: dict[str, Any], after: dict[str, Any]
) -> None:
    """``before`` / ``after``: the update's audit snapshots."""
    if all(before.get(f) == after.get(f) for f in MEANINGFUL_FIELDS):
        return
    status = after.get("status")
    if status == STATUS_SCHEDULED or (
        status == STATUS_CANCELLED and before.get("status") != STATUS_CANCELLED
    ):
        queue(db, appt.id)


def on_delete(db: DbSession, appt: Appointment) -> None:
    if appt.status != STATUS_SCHEDULED:
        return  # cancelled: already announced; done: in the past
    job = _pending(db, appt.id)
    if job is not None and (job.payload or {}).get("new"):
        # Never announced: nothing to take back.
        db.delete(job)
        db.flush()
        return
    queue(db, appt.id)


# ── The event to announce ────────────────────────────────────────────────────


def _deleted_event(db: DbSession, appointment_id: int) -> SimpleNamespace | None:
    """A stand-in for a deleted appointment, from its delete audit snapshot."""
    before = db.scalar(
        select(AuditLog.before)
        .where(
            AuditLog.entity == "appointment",
            AuditLog.entity_id == str(appointment_id),
            AuditLog.action == "delete",
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    if not isinstance(before, dict):
        return None
    try:
        doctor_id = before.get("doctor_id")
        return SimpleNamespace(
            id=appointment_id,
            patient_id=int(before["patient_id"]),
            doctor=db.get(Doctor, doctor_id) if doctor_id else None,
            starts_at=datetime.fromisoformat(before["starts_at"]),
            duration_min=int(before.get("duration_min") or 30),
            all_day=bool(before.get("all_day")),
            location=before.get("location"),
            reason=before.get("reason"),
            status=STATUS_CANCELLED,
            created_at=datetime.fromisoformat(before["created_at"]),
            updated_at=datetime.fromisoformat(before["updated_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def ends_at(event: Any) -> datetime:
    if event.all_day:
        return local_datetime(to_local(event.starts_at).date() + timedelta(days=1))
    return event.starts_at + timedelta(minutes=event.duration_min)


# ── Rendering ────────────────────────────────────────────────────────────────


def _one_line(value: str | None) -> str:
    return " ".join((value or "").split())


def _param(value: str) -> str:
    """An iCalendar parameter value: quoted, without DQUOTE or control chars."""
    clean = "".join(c for c in _one_line(value) if c != '"' and c.isprintable())
    return f'"{clean}"'


def when_short(event: Any) -> str:
    local = to_local(event.starts_at)
    day = f"{local.day} {MONTHS[local.month - 1]}"
    return day if event.all_day else f"{day} {local:%H:%M}"


def when_long(event: Any) -> str:
    local = to_local(event.starts_at)
    day = (
        f"{WEEKDAYS[local.weekday()]} {local.day} de {MONTHS_LONG[local.month - 1]} de {local.year}"
    )
    if event.all_day:
        return f"{day} (todo el día)"
    return f"{day}, {local:%H:%M}"


def _label(event: Any) -> str:
    reason = _one_line(event.reason)
    doctor = _one_line(event.doctor.name) if event.doctor is not None else ""
    if reason and doctor:
        return f"{reason} con {doctor}"
    return reason or doctor


def subject(event: Any, patient_name: str, *, method: str, new: bool) -> str:
    if method == METHOD_CANCEL:
        prefix = "Cita cancelada"
    else:
        prefix = "Cita" if new else "Cita actualizada"
    parts = [_one_line(patient_name), _label(event), when_short(event)]
    return f"{prefix}: " + " — ".join(p for p in parts if p)


def render_invite(
    event: Any,
    patient_name: str,
    base_url: str,
    *,
    method: str,
    seq: int,
    organizer: tuple[str, str],
    attendee: User,
    now: datetime,
) -> str:
    """The iTIP VCALENDAR for one recipient (CRLF, folded)."""
    org_name, org_addr = organizer
    extra = [
        f"ORGANIZER;CN={_param(org_name or 'n16chat')}:mailto:{org_addr}",
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE;"
        f"CN={_param(attendee.display_name)}:mailto:{attendee.email}",
    ]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//n16chat//Citas//ES",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        *VTIMEZONE,
        *vevent(
            event,
            patient_name,
            base_url,
            seq=seq,
            dtstamp=now,
            cancelled=method == METHOD_CANCEL,
            extra=extra,
        ),
        "END:VCALENDAR",
    ]
    return serialize(lines)


def _details(event: Any, patient_name: str) -> list[tuple[str, str]]:
    rows = [("Paciente", patient_name), ("Cuándo", when_long(event))]
    if event.doctor is not None:
        rows.append(("Médico", _one_line(event.doctor.name)))
    if event.reason:
        rows.append(("Motivo", _one_line(event.reason)))
    if event.location:
        rows.append(("Lugar", _one_line(event.location)))
    return rows


def build_message(
    settings: Settings,
    event: Any,
    patient_name: str,
    recipient: User,
    *,
    method: str,
    seq: int,
    new: bool,
    now: datetime,
) -> EmailMessage:
    org_name, org_addr = parseaddr(settings.email_from)
    ics = render_invite(
        event,
        patient_name,
        settings.app_base_url,
        method=method,
        seq=seq,
        organizer=(org_name, org_addr),
        attendee=recipient,
        now=now,
    )
    link = appointment_link(settings.app_base_url, event.id)
    settings_link = f"{settings.app_base_url.rstrip('/')}/calendario/suscribir"
    if method == METHOD_CANCEL:
        intro = "Esta cita se canceló. Tu calendario la quitará o la marcará como cancelada."
    elif new:
        intro = "Hay una cita nueva. Acéptala o ábrela para agregarla a tu calendario."
    else:
        intro = "Esta cita cambió. Tu calendario la actualizará al abrir la invitación."
    details = _details(event, patient_name)
    footer = (
        "Recibes este correo porque activaste «Recibir invitaciones por correo» en n16chat. "
        f"Puedes desactivarlo en {settings_link}"
    )
    text = (
        f"Hola {_one_line(recipient.display_name)}:\n\n{intro}\n\n"
        + "\n".join(f"{k}: {v}" for k, v in details)
        + f"\n\nVer en n16chat: {link}\n\n{footer}\n"
    )
    esc = html.escape
    rows = "".join(
        f'<tr><td style="padding: 2px 12px 2px 0; color: #64748b;">{esc(k)}</td>'
        f"<td>{esc(v)}</td></tr>"
        for k, v in details
    )
    body = f"""\
<!doctype html>
<html lang="es">
<body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; color: #0f172a;">
  <p>Hola {esc(_one_line(recipient.display_name))}:</p>
  <p>{esc(intro)}</p>
  <table style="border-collapse: collapse;">{rows}</table>
  <p><a href="{esc(link, quote=True)}" style="color: #106c69; font-weight: 600;">Ver en
     n16chat</a></p>
  <p style="color: #64748b; font-size: 14px;">Recibes este correo porque activaste
     «Recibir invitaciones por correo» en n16chat. Puedes desactivarlo en
     <a href="{esc(settings_link, quote=True)}">Calendario en tu teléfono</a>.</p>
</body>
</html>
"""
    msg = email_svc.build_message(
        settings,
        recipient.email,
        subject(event, patient_name, method=method, new=new),
        text,
        body,
    )
    data = ics.encode()
    # Inline part for the client's invitation UI, plus the same file as an
    # attachment for clients that only offer "open invite.ics".
    msg.add_alternative(
        data,
        maintype="text",
        subtype="calendar",
        params={"method": method, "charset": "UTF-8"},
    )
    msg.add_attachment(data, maintype="application", subtype="ics", filename="invite.ics")
    return msg


# ── The job ──────────────────────────────────────────────────────────────────


def recipients(db: DbSession) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.is_active.is_(True), User.email_invites.is_(True))
            .order_by(User.id)
        )
    )


@jobs.handler(KIND)
def send_invites(ctx: jobs.JobContext) -> None:
    db = ctx.db
    appointment_id = int(ctx.payload["appointment_id"])
    new = bool(ctx.payload.get("new"))
    appt = db.get(Appointment, appointment_id)
    event: Any
    if appt is None:
        event = _deleted_event(db, appointment_id)
        if event is None or new:
            return
        method, seq = METHOD_CANCEL, sequence(event) + 1
    elif appt.status == STATUS_DONE:
        return
    elif appt.status == STATUS_CANCELLED:
        if new:
            return
        event, method, seq = appt, METHOD_CANCEL, sequence(appt)
    else:
        event, method, seq = appt, METHOD_REQUEST, sequence(appt)

    now = utcnow()
    if ends_at(event) < now:
        logger.info("appointment %s has ended; no invite", appointment_id)
        return
    users = recipients(db)
    if not users:
        return

    settings = get_settings()
    patient = db.get(Patient, event.patient_id)
    patient_name = patient.display_name if patient is not None else "Paciente"
    messages = [
        build_message(settings, event, patient_name, user, method=method, seq=seq, new=new, now=now)
        for user in users
    ]
    if settings.email_dev_mode:
        logger.info(
            "DEV calendar invite (%s) for appointment %s to %d recipient(s); not sent",
            method,
            appointment_id,
            len(messages),
        )
        return
    if not settings.resend_api_key:
        logger.warning(
            "RESEND_API_KEY is not set; calendar invite for appointment %s not sent",
            appointment_id,
        )
        return
    for msg in messages:
        try:
            email_svc.send_message(settings, msg)
        except Exception:
            # No address, subject or exception text in the log or the job row.
            raise jobs.JobError(SEND_FAILED) from None
    logger.info(
        "calendar invite (%s) for appointment %s sent to %d recipient(s)",
        method,
        appointment_id,
        len(messages),
    )
