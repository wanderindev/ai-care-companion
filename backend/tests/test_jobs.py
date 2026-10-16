"""Background jobs: claiming with SKIP LOCKED, retries, failures, recovery, API.

Handlers here are throwaway test handlers registered per test; payloads and
messages are synthetic.
"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.models.job import Job
from app.models.user import User
from app.services import jobs


@pytest.fixture
def factory(test_db, test_engine):
    """A session factory on the test database (``test_db`` truncates at teardown)."""
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture
def register():
    """Register test handlers and remove them afterwards."""
    added: list[str] = []

    def _register(kind, fn):
        jobs.handler(kind)(fn)
        added.append(kind)
        return fn

    yield _register
    for kind in added:
        jobs.HANDLERS.pop(kind, None)


def _enqueue(factory, kind="test.noop", payload=None, **kw) -> int:
    with factory() as db:
        job = jobs.enqueue(db, kind, payload or {"item_id": 1}, **kw)
        db.commit()
        return job.id


def _get(factory, job_id) -> Job:
    with factory() as db:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job


def _make_due(factory, job_id) -> None:
    with factory() as db:
        db.execute(
            update(Job).where(Job.id == job_id).values(run_after=func.now() - timedelta(seconds=1))
        )
        db.commit()


def test_enqueue_is_part_of_the_callers_transaction(factory):
    with factory() as db:
        job = jobs.enqueue(db, "test.noop", {"item_id": 1})
        assert job.id is not None
        db.rollback()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_run_next_runs_a_job_to_done(factory, register):
    seen = {}

    def ok(ctx):
        seen.update(payload=ctx.payload, attempt=ctx.attempt, kind=ctx.kind)
        ctx.progress(50, "Procesando 1 de 2")

    register("test.ok", ok)
    job_id = _enqueue(factory, "test.ok", {"item_id": 7})

    assert jobs.run_next(factory) is True
    job = _get(factory, job_id)
    assert (job.status, job.progress, job.attempts) == ("done", 100, 1)
    assert job.message == "Procesando 1 de 2"
    assert job.locked_at is None and job.error is None
    assert seen == {"payload": {"item_id": 7}, "attempt": 1, "kind": "test.ok"}
    assert jobs.run_next(factory) is False


def test_progress_is_visible_while_the_job_runs(factory, register):
    observed = {}

    def slow(ctx):
        ctx.progress(30, "Paso 1")
        observed["job"] = _get(factory, ctx.job_id)

    register("test.slow", slow)
    job_id = _enqueue(factory, "test.slow")
    jobs.run_next(factory)

    mid = observed["job"]
    assert (mid.status, mid.progress, mid.message) == ("running", 30, "Paso 1")
    assert mid.locked_at is not None
    assert _get(factory, job_id).status == "done"


def test_handler_work_commits_with_done_and_rolls_back_on_failure(factory, register, test_db):
    def writes_then(fail):
        def fn(ctx):
            ctx.db.add(User(email=f"job{ctx.job_id}@example.com", display_name="Tarea"))
            if fail:
                raise RuntimeError("boom")

        return fn

    register("test.write_ok", writes_then(False))
    register("test.write_fail", writes_then(True))
    ok_id = _enqueue(factory, "test.write_ok")
    fail_id = _enqueue(factory, "test.write_fail")
    jobs.run_next(factory)
    jobs.run_next(factory)

    emails = set(test_db.scalars(select(User.email)))
    assert f"job{ok_id}@example.com" in emails
    assert f"job{fail_id}@example.com" not in emails


def test_failure_is_retried_with_backoff_then_failed(factory, register):
    calls = []

    def flaky(ctx):
        calls.append(ctx.attempt)
        raise ValueError("synthetic document text that must not be stored")

    register("test.flaky", flaky)
    job_id = _enqueue(factory, "test.flaky", max_attempts=2)

    assert jobs.run_next(factory) is True
    job = _get(factory, job_id)
    assert (job.status, job.attempts, job.error) == ("queued", 1, "ValueError")
    assert job.locked_at is None
    with factory() as db:
        delay = db.scalar(select(Job.run_after - func.now()).where(Job.id == job_id))
    assert timedelta(seconds=20) < delay <= jobs.retry_delay(1)

    # Not due yet: the worker leaves it alone.
    assert jobs.run_next(factory) is False
    _make_due(factory, job_id)
    assert jobs.run_next(factory) is True

    job = _get(factory, job_id)
    assert (job.status, job.attempts, job.error) == ("failed", 2, "ValueError")
    assert calls == [1, 2]
    assert "document" not in (job.error or "")


def test_retry_delay_grows_and_is_capped():
    assert jobs.retry_delay(1) == timedelta(seconds=30)
    assert jobs.retry_delay(2) == timedelta(seconds=60)
    assert jobs.retry_delay(3) == timedelta(seconds=120)
    assert jobs.retry_delay(20) == jobs.RETRY_MAX_DELAY


def test_job_error_message_is_recorded(factory, register):
    def fails(ctx):
        raise jobs.JobError("No se pudo leer el archivo.")

    register("test.job_error", fails)
    job_id = _enqueue(factory, "test.job_error")
    jobs.run_next(factory)
    job = _get(factory, job_id)
    assert job.status == "queued"
    assert job.error == "JobError: No se pudo leer el archivo."


def test_permanent_error_fails_without_retry(factory, register):
    def fails(ctx):
        raise jobs.PermanentJobError("Archivo no compatible.")

    register("test.permanent", fails)
    job_id = _enqueue(factory, "test.permanent", max_attempts=5)
    jobs.run_next(factory)
    job = _get(factory, job_id)
    assert (job.status, job.attempts) == ("failed", 1)
    assert job.error == "PermanentJobError: Archivo no compatible."


def test_unknown_kind_fails_at_once(factory):
    job_id = _enqueue(factory, "test.nobody_handles_this")
    jobs.run_next(factory)
    job = _get(factory, job_id)
    assert (job.status, job.attempts) == ("failed", 1)
    assert job.error.startswith("PermanentJobError")


def test_claim_skips_rows_locked_by_another_worker(factory):
    first = _enqueue(factory)
    second = _enqueue(factory)

    holder = factory()
    try:
        # Another worker is mid-claim on the first job (row locked, not committed).
        locked = holder.scalars(
            select(Job).where(Job.id == first).with_for_update(skip_locked=True)
        ).one()
        assert locked.id == first

        with factory() as db:
            claimed = jobs.claim_next(db)
            assert claimed is not None and claimed.id == second
            assert jobs.claim_next(db) is None  # the first is locked, the second running
    finally:
        holder.rollback()
        holder.close()

    with factory() as db:
        claimed = jobs.claim_next(db)
        assert claimed is not None and claimed.id == first
        assert (claimed.status, claimed.attempts) == ("running", 1)


def test_concurrent_workers_never_claim_the_same_job(factory):
    ids = {_enqueue(factory) for _ in range(6)}
    claimed: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def worker():
        barrier.wait()
        while True:
            with factory() as db:
                job = jobs.claim_next(db)
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(claimed) == sorted(ids)


def test_stuck_jobs_are_requeued_or_failed(factory):
    retry_id = _enqueue(factory, max_attempts=3)
    spent_id = _enqueue(factory, max_attempts=1)
    fresh_id = _enqueue(factory)
    with factory() as db:
        db.execute(
            update(Job)
            .where(Job.id.in_([retry_id, spent_id]))
            .values(status="running", attempts=1, locked_at=func.now() - timedelta(hours=1))
        )
        db.execute(
            update(Job)
            .where(Job.id == fresh_id)
            .values(status="running", attempts=1, locked_at=func.now())
        )
        db.commit()

    with factory() as db:
        assert jobs.recover_stuck_jobs(db, timedelta(minutes=15)) == 2

    retry, spent, fresh = (_get(factory, i) for i in (retry_id, spent_id, fresh_id))
    assert (retry.status, retry.error, retry.locked_at) == ("queued", "JobTimeout", None)
    assert (spent.status, spent.error) == ("failed", "JobTimeout")
    assert fresh.status == "running"


def test_get_job(auth_client, test_db):
    job = jobs.enqueue(test_db, "test.noop", {"item_id": 1}, message="En cola")
    test_db.commit()

    response = auth_client.get(f"/api/v1/jobs/{job.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["progress"] == 0
    assert body["message"] == "En cola"
    assert "payload" not in body

    assert auth_client.get("/api/v1/jobs/999999").status_code == 404


def test_get_job_requires_a_session(db_client, test_db):
    job = jobs.enqueue(test_db, "test.noop")
    test_db.commit()
    assert db_client.get(f"/api/v1/jobs/{job.id}").status_code == 401
