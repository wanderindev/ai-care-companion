"""Magic-link auth, sessions, CSRF and the allowlist. Synthetic users only."""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.api import auth as auth_api
from app.core.config import get_settings
from app.models.user import MagicLinkToken, Session, User
from app.services import auth as auth_service
from app.services.auth import hash_token, utcnow

XRW = {"X-Requested-With": "fetch"}
COOKIE = get_settings().session_cookie_name


@pytest.fixture
def sent(monkeypatch):
    """Captures magic-link emails instead of sending/logging them."""
    outbox: list[dict] = []

    def fake_send(settings, to, display_name, url):
        outbox.append({"to": to, "name": display_name, "url": url})

    monkeypatch.setattr(auth_api.auth_service, "send_magic_link_email", fake_send)
    return outbox


@pytest.fixture
def user(test_db):
    u = User(email="ana@example.com", display_name="Ana")
    test_db.add(u)
    test_db.commit()
    return u


def _token_from(url: str) -> str:
    assert "#token=" in url, url
    return url.split("#token=", 1)[1]


def _request(client, email, headers=None):
    return client.post(
        "/api/v1/auth/request-link", json={"email": email}, headers={**XRW, **(headers or {})}
    )


def _login(client, outbox, email="ana@example.com"):
    assert _request(client, email).status_code == 200
    token = _token_from(outbox[-1]["url"])
    resp = client.post("/api/v1/auth/verify", json={"token": token}, headers=XRW)
    assert resp.status_code == 200, resp.text
    return resp


# ── request-link ────────────────────────────────────────────────────────────


def test_request_link_sends_fragment_link_to_known_user(db_client, user, sent, test_db):
    resp = _request(db_client, "  Ana@Example.COM ")

    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0]["to"] == "ana@example.com"
    url = sent[0]["url"]
    assert url.startswith(get_settings().app_base_url + "/acceso#token=")
    assert "?" not in url  # token never in a query string (server logs)

    row = test_db.scalar(select(MagicLinkToken))
    token = _token_from(url)
    assert row.token_hash == hash_token(token)
    assert token not in row.token_hash  # stored hashed only
    assert row.user_id == user.id
    expiry = row.expires_at - row.created_at
    assert timedelta(minutes=14) < expiry <= timedelta(minutes=15, seconds=5)


def test_request_link_same_response_for_unknown_email(db_client, user, sent):
    known = _request(db_client, "ana@example.com")
    unknown = _request(db_client, "nobody@example.com")

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [m["to"] for m in sent] == ["ana@example.com"]


def test_request_link_inactive_user_gets_nothing(db_client, user, sent, test_db):
    user.is_active = False
    test_db.commit()

    resp = _request(db_client, "ana@example.com")

    assert resp.status_code == 200
    assert sent == []


def _ledger_count(test_db):
    return test_db.scalar(select(func.count()).select_from(MagicLinkToken))


def test_request_link_rate_limited_per_email(db_client, user, sent, test_db):
    for _ in range(auth_service.MAX_REQUESTS_PER_EMAIL):
        assert _request(db_client, "ana@example.com").status_code == 200
    over = _request(db_client, "ana@example.com")
    # Same 200 and body (no lockout error, no enumeration); nothing written or sent.
    assert over.status_code == 200
    assert over.json()["message"] == auth_api.LINK_SENT_MESSAGE
    assert len(sent) == auth_service.MAX_REQUESTS_PER_EMAIL
    assert _ledger_count(test_db) == auth_service.MAX_REQUESTS_PER_EMAIL
    # Unknown emails hit the same limit.
    for _ in range(auth_service.MAX_REQUESTS_PER_EMAIL + 1):
        assert _request(db_client, "ghost@example.com").status_code == 200
    assert _ledger_count(test_db) == 2 * auth_service.MAX_REQUESTS_PER_EMAIL


def test_request_link_rate_limited_per_ip(db_client, user, sent, test_db):
    edge = {"X-Real-IP": "203.0.113.7"}
    for i in range(auth_service.MAX_REQUESTS_PER_IP):
        assert _request(db_client, f"u{i}@example.com", edge).status_code == 200
    assert _request(db_client, "ana@example.com", edge).status_code == 200
    assert sent == []  # over the IP limit: the allowlisted user got nothing
    assert _ledger_count(test_db) == auth_service.MAX_REQUESTS_PER_IP
    # Another client IP is unaffected.
    other = {"X-Real-IP": "198.51.100.9"}
    assert _request(db_client, "ana@example.com", other).status_code == 200
    assert len(sent) == 1


def test_spoofed_x_forwarded_for_does_not_bypass_ip_limit(db_client, sent, test_db):
    """The edge overwrites X-Real-IP; a client-chosen X-Forwarded-For is ignored."""
    for i in range(auth_service.MAX_REQUESTS_PER_IP + 5):
        headers = {"X-Real-IP": "203.0.113.7", "X-Forwarded-For": f"1.2.3.{i}"}
        assert _request(db_client, f"u{i}@example.com", headers).status_code == 200
    assert _ledger_count(test_db) == auth_service.MAX_REQUESTS_PER_IP
    ips = set(test_db.scalars(select(MagicLinkToken.requested_ip)))
    assert ips == {"203.0.113.7"}


def test_oversized_or_invalid_ip_headers_are_dropped(db_client, user, sent, test_db):
    headers = {"X-Real-IP": "9" * 60, "X-Forwarded-For": "1" * 60}
    resp = _request(db_client, "ana@example.com", headers)

    assert resp.status_code == 200
    assert len(sent) == 1
    # TestClient's peer is "testclient" (not an IP), so nothing is stored.
    assert test_db.scalar(select(MagicLinkToken.requested_ip)) is None


def test_client_ip_normalizes_and_validates():
    from types import SimpleNamespace

    from app.api.deps import client_ip

    def req(headers, host):
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))

    assert client_ip(req({"x-real-ip": " 203.0.113.7 "}, "10.0.0.2")) == "203.0.113.7"
    assert client_ip(req({"x-real-ip": "2001:DB8::1"}, "10.0.0.2")) == "2001:db8::1"
    assert client_ip(req({}, "10.0.0.2")) == "10.0.0.2"
    # uvicorn's proxy-headers may put a client-chosen string in client.host.
    assert client_ip(req({"x-real-ip": "nope"}, "x" * 60)) is None
    assert client_ip(SimpleNamespace(headers={}, client=None)) is None


def test_rate_limit_window_expires(db_client, user, sent, test_db):
    for _ in range(auth_service.MAX_REQUESTS_PER_EMAIL):
        _request(db_client, "ana@example.com")
    test_db.execute(
        update(MagicLinkToken).values(created_at=utcnow() - auth_service.RATE_WINDOW * 2)
    )
    test_db.commit()

    assert _request(db_client, "ana@example.com").status_code == 200
    assert len(sent) == auth_service.MAX_REQUESTS_PER_EMAIL + 1


def test_dev_mode_logs_link_instead_of_sending(monkeypatch, caplog):
    settings = get_settings()
    assert settings.email_dev_mode  # ENVIRONMENT=test

    def boom(*args, **kwargs):
        raise AssertionError("must not send in dev mode")

    monkeypatch.setattr(auth_service, "send_email", boom)
    with caplog.at_level(logging.INFO, logger="app.auth"):
        auth_service.send_magic_link_email(
            settings, "ana@example.com", "Ana", "http://x/acceso#token=abc"
        )
    assert "http://x/acceso#token=abc" in caplog.text


def test_production_sends_spanish_email_over_resend_smtp(monkeypatch):
    settings = get_settings().model_copy(
        update={"environment": "production", "resend_api_key": "re_test_key"}
    )
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            calls["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, msg):
            calls["msg"] = msg

    monkeypatch.setattr("app.services.email.smtplib.SMTP_SSL", FakeSMTP)
    url = "https://app.example.com/acceso#token=abc"
    auth_service.send_magic_link_email(settings, "ana@example.com", "Ana", url)

    assert calls["server"] == ("smtp.resend.com", 465)
    assert calls["login"] == ("resend", "re_test_key")
    msg = calls["msg"]
    assert msg["To"] == "ana@example.com"
    assert msg["From"] == "n16chat <acceso@app.example.com>"
    assert msg["Subject"] == "Tu enlace para entrar a n16chat"
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert url in body and "Hola Ana" in body


def test_production_without_api_key_sends_nothing(monkeypatch):
    settings = get_settings().model_copy(update={"environment": "production"})

    def boom(*args, **kwargs):
        raise AssertionError("must not send without RESEND_API_KEY")

    monkeypatch.setattr(auth_service, "send_email", boom)
    auth_service.send_magic_link_email(settings, "ana@example.com", "Ana", "https://x#token=a")


# ── verify ──────────────────────────────────────────────────────────────────


def test_verify_sets_session_cookie_and_me_works(db_client, user, sent, test_db):
    resp = _login(db_client, sent)

    assert resp.json()["email"] == "ana@example.com"
    set_cookie = resp.headers["set-cookie"]
    assert f"{COOKIE}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Max-Age=2592000" in set_cookie  # 30 days

    me = db_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json() == {
        "id": user.id,
        "email": "ana@example.com",
        "display_name": "Ana",
        "role": "member",
        "is_admin": False,
        "can_import": False,
    }

    session = test_db.scalar(select(Session))
    assert session.token_hash == hash_token(db_client.cookies[COOKIE])


def test_verify_token_is_single_use(db_client, user, sent):
    _request(db_client, "ana@example.com")
    token = _token_from(sent[-1]["url"])

    first = db_client.post("/api/v1/auth/verify", json={"token": token}, headers=XRW)
    second = db_client.post("/api/v1/auth/verify", json={"token": token}, headers=XRW)

    assert first.status_code == 200
    assert second.status_code == 400


def test_verify_expired_token_rejected(db_client, user, sent, test_db):
    _request(db_client, "ana@example.com")
    token = _token_from(sent[-1]["url"])
    test_db.execute(update(MagicLinkToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    test_db.commit()

    resp = db_client.post("/api/v1/auth/verify", json={"token": token}, headers=XRW)

    assert resp.status_code == 400
    assert db_client.get("/api/v1/auth/me").status_code == 401


def test_verify_unknown_token_rejected(db_client):
    resp = db_client.post("/api/v1/auth/verify", json={"token": "nope"}, headers=XRW)
    assert resp.status_code == 400


def test_verify_rejected_when_user_deactivated_after_request(db_client, user, sent, test_db):
    _request(db_client, "ana@example.com")
    token = _token_from(sent[-1]["url"])
    user.is_active = False
    test_db.commit()

    resp = db_client.post("/api/v1/auth/verify", json={"token": token}, headers=XRW)
    assert resp.status_code == 400


def test_get_never_consumes_a_token(db_client, user, sent):
    """Email link scanners prefetch with GET; only the POST may consume."""
    _request(db_client, "ana@example.com")
    token = _token_from(sent[-1]["url"])

    assert db_client.get(f"/api/v1/auth/verify?token={token}").status_code == 405
    resp = db_client.post("/api/v1/auth/verify", json={"token": token}, headers=XRW)
    assert resp.status_code == 200


# ── sessions ────────────────────────────────────────────────────────────────


def test_me_requires_session(db_client):
    assert db_client.get("/api/v1/auth/me").status_code == 401


def test_session_slides_after_an_hour(db_client, user, sent, test_db):
    _login(db_client, sent)
    old = utcnow() - timedelta(hours=2)
    test_db.execute(update(Session).values(last_seen_at=old, expires_at=old + timedelta(days=30)))
    test_db.commit()

    resp = db_client.get("/api/v1/auth/me")

    assert resp.status_code == 200
    assert "Max-Age=2592000" in resp.headers["set-cookie"]
    test_db.expire_all()
    session = test_db.scalar(select(Session))
    assert session.last_seen_at > old + timedelta(hours=1)
    assert session.expires_at > utcnow() + timedelta(days=29, hours=23)


def test_session_not_refreshed_within_the_hour(db_client, user, sent, test_db):
    _login(db_client, sent)
    session = test_db.scalar(select(Session))
    before = (session.last_seen_at, session.expires_at)

    resp = db_client.get("/api/v1/auth/me")

    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers
    test_db.expire_all()
    session = test_db.scalar(select(Session))
    assert (session.last_seen_at, session.expires_at) == before


def test_expired_session_rejected_and_deleted(db_client, user, sent, test_db):
    _login(db_client, sent)
    test_db.execute(update(Session).values(expires_at=utcnow() - timedelta(seconds=1)))
    test_db.commit()

    assert db_client.get("/api/v1/auth/me").status_code == 401
    test_db.expire_all()
    assert test_db.scalar(select(Session)) is None


def test_deactivated_user_session_rejected(db_client, user, sent, test_db):
    _login(db_client, sent)
    user.is_active = False
    test_db.commit()

    assert db_client.get("/api/v1/auth/me").status_code == 401


def test_logout_deletes_session_and_clears_cookie(db_client, user, sent, test_db):
    _login(db_client, sent)
    token = db_client.cookies[COOKIE]

    resp = db_client.post("/api/v1/auth/logout", headers=XRW)

    assert resp.status_code == 204
    assert f'{COOKIE}=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]
    assert test_db.scalar(select(Session)) is None
    db_client.cookies.set(COOKIE, token)  # a stolen copy of the old cookie
    assert db_client.get("/api/v1/auth/me").status_code == 401


def test_cookie_secure_outside_local(monkeypatch):
    settings = get_settings()
    assert settings.cookie_secure is False  # ENVIRONMENT=test
    prod = settings.model_copy(update={"environment": "production"})
    assert prod.cookie_secure is True
    forced = settings.model_copy(update={"session_cookie_secure": True})
    assert forced.cookie_secure is True


# ── CSRF ────────────────────────────────────────────────────────────────────


def test_state_changing_requests_need_x_requested_with(db_client, user, sent):
    resp = db_client.post("/api/v1/auth/request-link", json={"email": "ana@example.com"})
    assert resp.status_code == 403
    assert sent == []
    assert db_client.post("/api/v1/auth/logout").status_code == 403


def test_get_requests_do_not_need_the_header(client):
    assert client.get("/api/v1/health").status_code == 200


# ── require_admin ───────────────────────────────────────────────────────────


def test_require_admin(db_client, user, sent, test_db):
    from fastapi import Depends

    from app.api.deps import require_admin
    from app.main import app

    @app.get("/api/v1/_test-admin")
    def _admin_only(u: User = Depends(require_admin)):
        return {"ok": True}

    try:
        _login(db_client, sent)
        assert db_client.get("/api/v1/_test-admin").status_code == 403
        user.role = "admin"
        test_db.commit()
        assert db_client.get("/api/v1/_test-admin").status_code == 200
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/api/v1/_test-admin"
        ]
