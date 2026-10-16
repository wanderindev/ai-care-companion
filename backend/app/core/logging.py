"""Logging setup shared by the API and the worker.

Two rules: logs never contain medical content, and secrets that travel in
URLs never reach ``docker logs``. The per-user calendar feed token is part of
its path (``/api/v1/calendar/<token>.ics``) and a magic-link token may arrive
as a ``?token=`` query parameter, so uvicorn's access log is redacted.
"""

import logging
import re

REDACTED = "[REDACTED]"

# /api/v1/calendar/<token>.ics -> /api/v1/calendar/[REDACTED].ics
_CALENDAR_TOKEN_RE = re.compile(r"(/calendar/)[^/?#\s\"]+(\.ics)")
# ?token=... / &token=... (also magic_token=, login_token=, ...) -> token=[REDACTED]
_TOKEN_PARAM_RE = re.compile(r"([?&][\w-]*token=)[^&#\s\"]*", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Strip URL-borne secrets (calendar feed tokens, token query params)."""
    text = _CALENDAR_TOKEN_RE.sub(rf"\1{REDACTED}\2", text)
    return _TOKEN_PARAM_RE.sub(rf"\1{REDACTED}", text)


class RedactSecretsFilter(logging.Filter):
    """Rewrites a record's message and arguments through :func:`redact_secrets`.

    uvicorn logs access lines as ``'%s - "%s %s HTTP/%s" %d'`` with the path
    (including the query string) as an argument, so the arguments are what
    needs redacting.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_secrets(a) if isinstance(a, str) else a for a in record.args)
        return True


def install_access_log_redaction() -> None:
    """Attach the redaction filter to uvicorn's loggers (idempotent).

    Runs when ``app.main`` is imported, which uvicorn does in every worker
    process *after* applying its own logging config, so the filter sticks.
    """
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactSecretsFilter) for f in logger.filters):
            logger.addFilter(RedactSecretsFilter())


def configure_logging(level_name: str) -> None:
    """Root handler at WARNING; only the ``app`` namespace is raised.

    Keeps third-party libraries quiet on the shared droplet while letting
    ``app.*`` loggers reach ``docker logs``. Also installs the access-log
    redaction.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    logging.getLogger("app").setLevel(level)
    install_access_log_redaction()
