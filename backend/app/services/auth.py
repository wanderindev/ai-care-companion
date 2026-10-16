"""Magic-link login and cookie sessions (docs/DESIGN.md §2).

- Tokens are 256-bit random strings; only their SHA-256 is stored.
- Magic links live 15 minutes and are consumed by an atomic UPDATE, so a
  token can be used once even under concurrent requests.
- Sessions slide: an authenticated request more than an hour after the last
  refresh pushes ``expires_at`` to now + ``session_days``.
- Rate limits are counted from ``magic_link_tokens`` rows (shared by every
  uvicorn worker), per normalized email and per client IP (the edge's
  X-Real-IP, see ``app.api.deps.client_ip``). Over a limit nothing is
  written or sent, and the API still answers 200.
"""

import hashlib
import html
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings
from app.models.user import MagicLinkToken, Session, User
from app.services.email import send_email

logger = logging.getLogger("app.auth")

MAGIC_LINK_TTL = timedelta(minutes=15)
SESSION_REFRESH_INTERVAL = timedelta(hours=1)

# Rate limits on POST /auth/request-link.
RATE_WINDOW = timedelta(hours=1)
MAX_REQUESTS_PER_EMAIL = 5
MAX_REQUESTS_PER_IP = 20
# Ledger rows older than this are pruned opportunistically on each request.
LEDGER_RETENTION = timedelta(days=7)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


# ── Magic links ─────────────────────────────────────────────────────────────


class RateLimited(Exception):
    pass


def check_rate_limit(db: DbSession, email: str, ip: str | None) -> None:
    since = utcnow() - RATE_WINDOW
    by_email = db.scalar(
        select(func.count())
        .select_from(MagicLinkToken)
        .where(MagicLinkToken.email == email, MagicLinkToken.created_at >= since)
    )
    if by_email >= MAX_REQUESTS_PER_EMAIL:
        raise RateLimited
    if ip:
        by_ip = db.scalar(
            select(func.count())
            .select_from(MagicLinkToken)
            .where(MagicLinkToken.requested_ip == ip, MagicLinkToken.created_at >= since)
        )
        if by_ip >= MAX_REQUESTS_PER_IP:
            raise RateLimited


def create_magic_link(db: DbSession, email: str, ip: str | None) -> tuple[User | None, str]:
    """Record a link request; returns ``(user, token)``.

    ``user`` is None for unknown or inactive emails: the row is still written
    (it feeds the rate limits) but the caller must not send anything.
    Raises :class:`RateLimited` before writing when a limit is exceeded.
    """
    email = normalize_email(email)
    now = utcnow()
    db.execute(delete(MagicLinkToken).where(MagicLinkToken.created_at < now - LEDGER_RETENTION))
    check_rate_limit(db, email, ip)

    user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    token = new_token()
    db.add(
        MagicLinkToken(
            user_id=user.id if user else None,
            email=email,
            token_hash=hash_token(token),
            expires_at=now + MAGIC_LINK_TTL,
            requested_ip=ip,
        )
    )
    db.commit()
    return user, token


def consume_magic_link(db: DbSession, token: str) -> User | None:
    """Mark the token used and return its (active) user, or None if invalid.

    Invalid covers unknown, expired, already used, and tokens whose user is
    missing or deactivated.
    """
    now = utcnow()
    user_id = db.scalar(
        update(MagicLinkToken)
        .where(
            MagicLinkToken.token_hash == hash_token(token),
            MagicLinkToken.used_at.is_(None),
            MagicLinkToken.expires_at > now,
            MagicLinkToken.user_id.is_not(None),
        )
        .values(used_at=now)
        .returning(MagicLinkToken.user_id)
    )
    db.commit()
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def magic_link_url(settings: Settings, token: str) -> str:
    # The token travels in the fragment, which browsers never send to a
    # server, so it cannot land in any access log. A plain GET of the link
    # only loads the SPA; the SPA POSTs the token to /auth/verify.
    return f"{settings.app_base_url.rstrip('/')}/acceso#token={token}"


def send_magic_link_email(settings: Settings, to: str, display_name: str, url: str) -> None:
    """Send (or, in local environments, log) the sign-in email.

    Runs as a background task after the response, so the response time does
    not reveal whether the email is on the allowlist.
    """
    if settings.email_dev_mode:
        logger.info("DEV magic link for %s: %s", to, url)
        return
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY is not set; magic-link email not sent")
        return

    minutes = int(MAGIC_LINK_TTL.total_seconds() // 60)
    subject = "Tu enlace para entrar a n16chat"
    text = (
        f"Hola {display_name}:\n\n"
        f"Usa este enlace para entrar a n16chat. Vence en {minutes} minutos y "
        "solo funciona una vez.\n\n"
        f"{url}\n\n"
        "Si no pediste este enlace, ignora este correo.\n"
    )
    safe_url = html.escape(url, quote=True)
    body = f"""\
<!doctype html>
<html lang="es">
<body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; color: #0f172a;">
  <p>Hola {html.escape(display_name)}:</p>
  <p>Usa este botón para entrar a <strong>n16chat</strong>. El enlace vence en
     {minutes} minutos y solo funciona una vez.</p>
  <p><a href="{safe_url}" style="display: inline-block; padding: 12px 24px;
     background: #106c69; color: #ffffff; border-radius: 9999px;
     text-decoration: none; font-weight: 600;">Entrar a n16chat</a></p>
  <p style="color: #64748b; font-size: 14px;">Si no pediste este enlace,
     ignora este correo.</p>
</body>
</html>
"""
    try:
        send_email(settings, to, subject, text, body)
    except Exception:
        # No address or link in the log line; the user can request another.
        logger.exception("Sending the magic-link email failed")


# ── Sessions ────────────────────────────────────────────────────────────────


def create_session(
    db: DbSession, settings: Settings, user: User, user_agent: str | None
) -> tuple[Session, str]:
    now = utcnow()
    token = new_token()
    session = Session(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=now + timedelta(days=settings.session_days),
        last_seen_at=now,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(session)
    db.commit()
    return session, token


def get_valid_session(db: DbSession, token: str) -> Session | None:
    """The live session for ``token`` with an active user, else None.

    Expired sessions are deleted when seen.
    """
    session = db.scalar(select(Session).where(Session.token_hash == hash_token(token)))
    if session is None:
        return None
    if session.expires_at <= utcnow():
        db.delete(session)
        db.commit()
        return None
    if not session.user.is_active:
        return None
    return session


def refresh_session_if_due(db: DbSession, settings: Settings, session: Session) -> bool:
    """Slide the expiry, at most once per hour per session. True if refreshed."""
    now = utcnow()
    if now - session.last_seen_at < SESSION_REFRESH_INTERVAL:
        return False
    session.last_seen_at = now
    session.expires_at = now + timedelta(days=settings.session_days)
    db.commit()
    return True


def delete_session(db: DbSession, token: str) -> None:
    db.execute(delete(Session).where(Session.token_hash == hash_token(token)))
    db.commit()
