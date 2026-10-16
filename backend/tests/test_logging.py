import logging

import pytest

from app.core.logging import (
    REDACTED,
    RedactSecretsFilter,
    install_access_log_redaction,
    redact_secrets,
)

# Synthetic tokens only.
FAKE_TOKEN = "s3cr3t-Fake_Token-0123456789abcdef"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"/api/v1/calendar/{FAKE_TOKEN}.ics", f"/api/v1/calendar/{REDACTED}.ics"),
        (
            f"/api/v1/calendar/{FAKE_TOKEN}.ics?x=1",
            f"/api/v1/calendar/{REDACTED}.ics?x=1",
        ),
        (f"/login/verify?token={FAKE_TOKEN}", f"/login/verify?token={REDACTED}"),
        (
            f"/auth?next=%2F&magic_token={FAKE_TOKEN}&a=b",
            f"/auth?next=%2F&magic_token={REDACTED}&a=b",
        ),
        (f"/x?TOKEN={FAKE_TOKEN}", f"/x?TOKEN={REDACTED}"),
        # Untouched: no secret in these.
        ("/api/v1/health", "/api/v1/health"),
        ("/api/v1/calendar/feed", "/api/v1/calendar/feed"),
        ("/api/v1/log-entries?patient=papa", "/api/v1/log-entries?patient=papa"),
    ],
)
def test_redact_secrets(raw, expected):
    assert redact_secrets(raw) == expected


def test_filter_redacts_uvicorn_access_record_args():
    # Same shape uvicorn's access logger emits.
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.18.0.5:5000", "GET", f"/api/v1/calendar/{FAKE_TOKEN}.ics", "1.1", 200),
        exc_info=None,
    )

    assert RedactSecretsFilter().filter(record) is True
    message = record.getMessage()
    assert FAKE_TOKEN not in message
    assert f"/api/v1/calendar/{REDACTED}.ics" in message
    assert message.endswith("200")


def test_filter_is_installed_on_uvicorn_access_logger_once():
    import app.main  # noqa: F401  -- importing the app installs the filter

    install_access_log_redaction()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, RedactSecretsFilter) for f in filters) == 1


def test_uvicorn_access_logger_output_is_redacted(caplog):
    import app.main  # noqa: F401  -- importing the app installs the filter

    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            f"/?token={FAKE_TOKEN}",
            "1.1",
            200,
        )
    assert FAKE_TOKEN not in caplog.text
    assert f"token={REDACTED}" in caplog.text
