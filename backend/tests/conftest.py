"""Pytest configuration and shared fixtures.

Tests that need a database get a real PostgreSQL 17 from testcontainers (an
ephemeral sibling container via the docker socket). Fixtures and test data are
synthetic only: never real patient names, ID numbers or medical text.
"""

import os

# Set before importing the app so nothing reads a production-like environment.
os.environ["ENVIRONMENT"] = "test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from testcontainers.community.postgres import PostgresContainer  # noqa: E402

import app.models  # noqa: E402, F401  -- registers every model
from app.core.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def postgres_container():
    """One PostgreSQL 17 container for the whole session (started lazily)."""
    with PostgresContainer("postgres:17-alpine") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def test_engine(postgres_container):
    engine = create_engine(postgres_container.get_connection_url(), pool_pre_ping=True)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def _reset_database(engine) -> None:
    """Truncate every table and reset sequences between tests."""
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if not tables:
        return
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def test_db(test_engine):
    """A real, committing session; the database is truncated at teardown."""
    session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)()
    try:
        yield session
    finally:
        session.close()
        _reset_database(test_engine)


@pytest.fixture
def client():
    """Test client for endpoints that do not touch the database."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_client(test_db):
    """Test client whose ``get_db`` dependency uses the test database."""

    def override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


# ── Signed-in clients, patients and storage (synthetic data only) ───────────


@pytest.fixture
def member(test_db):
    from app.models.user import User

    user = User(email="ana@example.com", display_name="Ana")
    test_db.add(user)
    test_db.commit()
    return user


@pytest.fixture
def auth_client(db_client, test_db, member):
    """``db_client`` signed in as ``member``; sends X-Requested-With on every call."""
    from app.core.config import get_settings
    from app.services.auth import create_session

    settings = get_settings()
    _, token = create_session(test_db, settings, member, "pytest")
    db_client.cookies.set(settings.session_cookie_name, token)
    db_client.headers["X-Requested-With"] = "fetch"
    return db_client


@pytest.fixture
def patients(test_db):
    """The two rows migration 0003 seeds (display names only)."""
    from app.models.patient import Patient

    rows = [Patient(slug="papa", display_name="Papá"), Patient(slug="mama", display_name="Mamá")]
    test_db.add_all(rows)
    test_db.commit()
    return rows


@pytest.fixture
def storage():
    """An in-process S3 (moto) bucket behind the attachments' storage dependency."""
    import boto3
    from moto import mock_aws

    from app.api.attachments import storage_dep
    from app.core.config import Settings
    from app.services.storage import Storage

    settings = Settings(
        do_spaces_key="test-key",
        do_spaces_secret="test-secret",
        do_spaces_bucket="n16chat-test",
        do_spaces_endpoint="https://s3.amazonaws.com",
        do_spaces_region="us-east-1",
        do_spaces_public_endpoint="http://localhost:9010",
    )
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="n16chat-test")
        store = Storage(settings)
        app.dependency_overrides[storage_dep] = lambda: store
        try:
            yield store
        finally:
            app.dependency_overrides.pop(storage_dep, None)


class FakePatientLLM:
    """Stands in for Claude in lab patient matching (#98). Answers
    ``self.answer`` (default: no patient, low confidence); records each call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.answer: dict | str = {"patient": None, "confidence": "low", "reason_code": "ambiguous"}
        self.error: Exception | None = None

    def create(self, **params):
        import json
        from types import SimpleNamespace

        self.calls.append(params)
        if self.error is not None:
            raise self.error
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=400,
                output_tokens=20,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


@pytest.fixture(autouse=True)
def patient_llm(monkeypatch):
    """Lab patient matching never reaches the real API in tests."""
    from types import SimpleNamespace

    from app.services.labs import patient_llm as module

    fake = FakePatientLLM()
    monkeypatch.setattr(
        module, "make_client", lambda key: SimpleNamespace(beta=SimpleNamespace(messages=fake))
    )
    return fake
