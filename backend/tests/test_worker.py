import threading

from sqlalchemy.orm import sessionmaker

from app.models.job import Job
from app.services import jobs
from app.worker import load_handlers, run


def _wait_for(predicate, tries=500):
    for _ in range(tries):
        if predicate():
            return True
        threading.Event().wait(0.01)
    return False


def test_worker_runs_jobs_touches_heartbeat_and_stops(tmp_path, test_db, test_engine):
    factory = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    jobs.handler("test.worker_loop")(lambda ctx: ctx.progress(10, "Paso"))
    try:
        job = jobs.enqueue(test_db, "test.worker_loop")
        test_db.commit()

        heartbeat = tmp_path / "heartbeat"
        stop = threading.Event()
        thread = threading.Thread(
            target=run,
            args=(stop, factory),
            kwargs={"heartbeat_path": heartbeat, "heartbeat_interval": 0.01, "poll_interval": 0.01},
        )
        thread.start()
        try:
            assert _wait_for(heartbeat.exists)

            def done():
                test_db.expire_all()
                return test_db.get(Job, job.id).status == "done"

            assert _wait_for(done)
        finally:
            stop.set()
            thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        jobs.HANDLERS.pop("test.worker_loop", None)


def test_worker_survives_database_errors(tmp_path):
    class Broken:
        def __call__(self):
            raise RuntimeError("database unavailable")

    stop = threading.Event()
    thread = threading.Thread(
        target=run,
        args=(stop, Broken()),
        kwargs={"heartbeat_path": tmp_path / "hb", "error_backoff": 0.01},
    )
    thread.start()
    threading.Event().wait(0.1)
    assert thread.is_alive()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_load_handlers_imports_modules():
    load_handlers(("app.services.jobs",))
