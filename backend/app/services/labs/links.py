"""Lab portal links shared in the chat (DESIGN §5.5, §6.5; #16).

Detection (``find_lab_urls``): every ``http(s)://`` URL in a message's text,
trailing punctuation trimmed, normalized (``normalize_url``: lowercase scheme
and host, default port and fragment dropped; path and query kept as written,
since portal tokens are case-sensitive). A URL is a lab link when its host
matches the ``LAB_LINK_HOSTS`` allow-list, or when its path or query mentions
``resultado(s)`` / ``laboratorio(s)`` (anywhere in a word) or has a word
``pdf`` / ``lab`` / ``labs`` (accents and case ignored). Links are deduped on
the SHA-256 of the normalized URL (``lab_links.url_hash``).

Recording (``record_links``): one ``lab_links`` row per new URL, with the
message and a patient guess (``guess_patient``: the text names exactly one
patient by display name or slug — "papá", "mamá" — else none; it is only a
hint shown in the list). A link is queued for the worker (``lab_link_fetch``
job, payload ``{"lab_link_id": id}`` only) when it is ``https`` and its host
is allow-listed; otherwise it stays ``pending`` with reason ``not_allowed``
for someone to open by hand.

Entry points: the WhatsApp ``after_import`` hook (``labs.jobs``) for each new
import's messages, and ``python -m scripts.lab_links scan`` for messages
imported before #16 (it also queues allow-listed pending links never tried,
e.g. after ``LAB_LINK_HOSTS`` changed).

PHI: URLs may carry patient tokens. They live in the database only — never
in logs, job payloads or job messages.
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings
from app.models.lab import LabLink
from app.models.patient import Patient
from app.models.whatsapp import WhatsappMessage
from app.services import audit, jobs
from app.services.labs.fetch import host_allowed
from app.services.labs.values import strip_accents

FETCH_JOB_KIND = "lab_link_fetch"
FETCH_MAX_ATTEMPTS = 3
URL_MAX = 2048
NOT_ALLOWED = "not_allowed"

_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>»…\"'*_~"
_WORDS = re.compile(r"[a-z0-9]+")
_STEMS = ("resultado", "laboratorio")
_WORD_HINTS = frozenset({"pdf", "lab", "labs"})


def normalize_url(raw: str) -> tuple[str, str] | None:
    """``(normalized url, host)``, or None when it is not a usable web URL."""
    raw = raw.strip()
    if len(raw) > URL_MAX:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    if scheme not in ("http", "https") or not host:
        return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    default = 443 if scheme == "https" else 80
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != default:
        netloc = f"{netloc}:{port}"
    if parts.username or parts.password:
        userinfo = parts.netloc.rsplit("@", 1)[0]
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, "")), host


def url_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def looks_like_lab_url(normalized: str, host: str, patterns: tuple[str, ...]) -> bool:
    if host_allowed(host, patterns):
        return True
    parts = urlsplit(normalized)
    text = strip_accents(unquote(f"{parts.path}?{parts.query}")).lower()
    words = _WORDS.findall(text)
    return any(stem in w for w in words for stem in _STEMS) or any(w in _WORD_HINTS for w in words)


def find_lab_urls(text: str | None, patterns: tuple[str, ...]) -> list[tuple[str, str]]:
    """The lab links in ``text`` as ``(normalized url, host)``, in order, deduped."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _URL.finditer(text or ""):
        candidate = match.group(0).rstrip(_TRAILING)
        normalized = normalize_url(candidate)
        if normalized is None or normalized[0] in seen:
            continue
        if looks_like_lab_url(*normalized, patterns):
            seen.add(normalized[0])
            found.append(normalized)
    return found


def _key(value: str | None) -> str:
    return strip_accents(value or "").lower().strip()


def guess_patient(text: str | None, patients: Sequence[Patient]) -> int | None:
    """The one patient the text names (display name or slug, whole word), else None."""
    words = set(_WORDS.findall(_key(text)))
    named = [
        p.id
        for p in patients
        if {k for k in (_key(p.display_name), _key(p.slug)) if k and " " not in k} & words
    ]
    return named[0] if len(named) == 1 else None


def fetchable(link: LabLink, settings: Settings) -> bool:
    """Whether the worker may try ``link`` (https and an allow-listed host)."""
    return link.url.startswith("https://") and host_allowed(
        link.host, settings.lab_link_host_patterns
    )


def enqueue_fetch(db: DbSession, link: LabLink) -> None:
    """Queue a ``lab_link_fetch`` job (ids only; caller commits)."""
    job = jobs.enqueue(
        db,
        FETCH_JOB_KIND,
        {"lab_link_id": link.id},
        max_attempts=FETCH_MAX_ATTEMPTS,
        message="En cola",
    )
    link.job_id = job.id
    link.reason = None


def record_links(
    db: DbSession, messages: Iterable[WhatsappMessage], settings: Settings
) -> list[LabLink]:
    """Add a ``lab_links`` row for every new lab URL in ``messages`` and queue
    the fetchable ones (caller commits). Returns the new rows."""
    patterns = settings.lab_link_host_patterns
    candidates: list[tuple[WhatsappMessage, str, str, str]] = []
    for message in messages:
        for normalized, host in find_lab_urls(message.text, patterns):
            candidates.append((message, normalized, host, url_hash(normalized)))
    if not candidates:
        return []
    hashes = {c[3] for c in candidates}
    seen = set(db.scalars(select(LabLink.url_hash).where(LabLink.url_hash.in_(hashes))))
    patients = list(db.scalars(select(Patient).order_by(Patient.id)))
    created: list[LabLink] = []
    for message, normalized, host, digest in candidates:
        if digest in seen:
            continue
        seen.add(digest)
        link = LabLink(
            url=normalized,
            url_hash=digest,
            host=host[:255],
            message_id=message.id,
            patient_id=guess_patient(message.text, patients),
            status="pending",
        )
        db.add(link)
        db.flush()
        if fetchable(link, settings):
            enqueue_fetch(db, link)
        else:
            link.reason = NOT_ALLOWED
        db.flush()
        audit.record(
            db,
            user_id=None,
            action=audit.ACTION_CREATE,
            entity="lab_link",
            entity_id=link.id,
            after=audit.snapshot(link),
        )
        created.append(link)
    return created


def enqueue_untried(db: DbSession, settings: Settings) -> int:
    """Queue pending links that were never tried and are fetchable now (e.g.
    after ``LAB_LINK_HOSTS`` changed). Caller commits. Returns the count."""
    count = 0
    for link in db.scalars(
        select(LabLink)
        .where(LabLink.status == "pending", LabLink.job_id.is_(None))
        .order_by(LabLink.id)
    ):
        if fetchable(link, settings):
            enqueue_fetch(db, link)
            count += 1
    return count
