"""Derived medication history (ticket #28): the backfill from imported
bitácora entries and the review API. Synthetic data only."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditLog
from app.models.medication import MedEvent
from app.services import log_entries as entries_svc
from app.services.med_events_backfill import KIND_MAP, backfill


def url(patient, suffix=""):
    return f"/api/v1/patients/{patient.id}/meds/events{suffix}"


def _entry(test_db, patient, day, events=..., index=0, origin="import_docx"):
    origin_ref = {"source_sha256": "0" * 64, "entry_index": index, "text_sha256": str(index)}
    if events is not ...:
        origin_ref["medication_events"] = events
    return entries_svc.create_entry(
        test_db,
        {
            "patient_id": patient.id,
            "entry_date": day,
            "entry_type": "consulta_control",
            "title": "Control sintético",
            "review_status": "pending_review",
        },
        user_id=None,
        origin=origin,
        origin_ref=origin_ref if origin == "import_docx" else None,
    )


def _ev(kind, medication="Medicina A", detail="1 en el desayuno sintético"):
    return {"kind": kind, "medication": medication, "detail": detail}


@pytest.fixture
def imported(test_db, patients):
    papa, mama = patients
    rows = {
        "multi": _entry(
            test_db,
            papa,
            date(2025, 1, 10),
            [
                _ev("start"),
                _ev("stop", "Medicina B", None),
                _ev("change", "Medicina C", "  "),
                _ev("substitution", "Medicina D por Medicina E"),
            ],
            1,
        ),
        "skips": _entry(
            test_db,
            mama,
            date(2025, 2, 1),
            [
                _ev("pause"),  # unknown kind
                _ev("start", "   "),  # no medication
                _ev("start", "X" * 201),  # longer than the column
                "not an event",
                _ev("start", "Medicina Z"),
            ],
            2,
        ),
        "empty": _entry(test_db, papa, date(2025, 3, 1), [], 3),
        "no_key": _entry(test_db, papa, date(2025, 3, 2), ..., 4),
    }
    # A manual entry is never a source, whatever its origin_ref says.
    manual = _entry(test_db, papa, date(2025, 3, 3), origin="manual")
    manual.origin_ref = {"medication_events": [_ev("start")]}
    test_db.commit()
    return {k: v.id for k, v in rows.items()}


def _events(test_db):
    test_db.expire_all()
    return test_db.scalars(
        select(MedEvent).order_by(MedEvent.source_entry_id, MedEvent.source_index)
    ).all()


# ── Backfill ─────────────────────────────────────────────────────────────────


def test_kind_map_covers_extractor_kinds():
    from app.models.medication import EVENT_KINDS
    from app.services.bitacora_import.extract import MED_EVENT_KINDS

    assert set(KIND_MAP) == set(MED_EVENT_KINDS)
    assert set(KIND_MAP.values()) == set(EVENT_KINDS)


def test_backfill_creates_and_maps(test_db, patients, imported, member):
    papa, mama = patients
    report = backfill(test_db, user_id=member.id)
    test_db.commit()
    assert (report.entries, report.events, report.created) == (2, 9, 5)
    assert (report.unknown_kind, report.invalid, report.existing, report.deleted_before) == (
        1,
        3,
        0,
        0,
    )
    rows = _events(test_db)
    assert [(r.source_entry_id, r.source_index, r.kind) for r in rows] == [
        (imported["multi"], 0, "inicio"),
        (imported["multi"], 1, "suspension"),
        (imported["multi"], 2, "cambio_dosis"),
        (imported["multi"], 3, "sustitucion"),
        (imported["skips"], 4, "inicio"),
    ]
    first = rows[0]
    assert first.patient_id == papa.id and first.event_date == date(2025, 1, 10)
    assert first.medication == "Medicina A" and first.detail == "1 en el desayuno sintético"
    assert rows[1].detail is None and rows[2].detail is None  # null and blank
    assert rows[4].patient_id == mama.id and rows[4].event_date == date(2025, 2, 1)
    assert {r.review_status for r in rows} == {"pending_review"}
    assert {r.created_by for r in rows} == {member.id}
    audits = test_db.scalars(select(AuditLog).where(AuditLog.entity == "med_event")).all()
    assert len(audits) == 5 and {a.action for a in audits} == {"create"}


def test_backfill_is_idempotent_after_review(auth_client, test_db, patients, imported, member):
    papa = patients[0]
    backfill(test_db, user_id=member.id)
    test_db.commit()
    rows = _events(test_db)
    # Confirm one, edit another, discard a third through the API.
    assert auth_client.patch(url(papa, f"/{rows[0].id}"), json={"review_status": "ok"}).is_success
    assert auth_client.patch(
        url(papa, f"/{rows[1].id}"), json={"medication": "Medicina B2"}
    ).is_success
    assert auth_client.delete(url(papa, f"/{rows[2].id}")).status_code == 204

    again = backfill(test_db, user_id=member.id)
    test_db.commit()
    assert (again.created, again.existing, again.deleted_before) == (0, 4, 1)
    after = _events(test_db)
    assert len(after) == 4
    by_index = {(r.source_entry_id, r.source_index): r for r in after}
    assert by_index[(imported["multi"], 0)].review_status == "ok"
    assert by_index[(imported["multi"], 1)].medication == "Medicina B2"
    assert (imported["multi"], 2) not in by_index


def test_backfill_script_dry_run_and_run(
    test_engine, test_db, imported, member, monkeypatch, capsys
):
    from scripts import med_events as script

    monkeypatch.setattr(script, "SessionLocal", sessionmaker(bind=test_engine))
    assert script.main(["backfill", "--by", "ANA@example.com", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run, nothing written" in out
    assert "would create 5" in out
    assert "Medicina" not in out and "sintético" not in out  # counts only
    assert _events(test_db) == []

    assert script.main(["backfill", "--by", "ana@example.com"]) == 0
    out = capsys.readouterr().out
    assert "created 5" in out and "Medicina" not in out
    assert len(_events(test_db)) == 5

    assert script.main(["backfill", "--by", "ana@example.com"]) == 0
    assert "created 0" in capsys.readouterr().out
    assert script.main(["backfill", "--by", "nadie@example.com"]) == 1


# ── Review API ───────────────────────────────────────────────────────────────


@pytest.fixture
def pending(test_db, patients, imported, member):
    backfill(test_db, user_id=member.id)
    test_db.commit()
    return _events(test_db)


def test_review_requires_auth(db_client, patients, pending):
    papa = patients[0]
    eid = pending[0].id
    assert (
        db_client.patch(
            url(papa, f"/{eid}"), json={"review_status": "ok"}, headers={"X-Requested-With": "x"}
        ).status_code
        == 401
    )
    assert db_client.delete(
        url(papa, f"/{eid}"), headers={"X-Requested-With": "x"}
    ).status_code == (401)
    assert (
        db_client.post(
            url(papa, "/confirm"), json={"ids": [eid]}, headers={"X-Requested-With": "x"}
        ).status_code
        == 401
    )


def test_review_requires_csrf_header(auth_client, patients, pending):
    papa = patients[0]
    del auth_client.headers["X-Requested-With"]
    eid = pending[0].id
    assert auth_client.patch(url(papa, f"/{eid}"), json={"review_status": "ok"}).status_code == 403
    assert auth_client.delete(url(papa, f"/{eid}")).status_code == 403
    assert auth_client.post(url(papa, "/confirm"), json={"ids": [eid]}).status_code == 403


def test_edit_and_confirm(auth_client, test_db, patients, pending, member):
    papa = patients[0]
    event = pending[0]
    resp = auth_client.patch(
        url(papa, f"/{event.id}"),
        json={
            "event_date": "2025-01-12",
            "kind": "cambio_dosis",
            "medication": "  Medicina A2 ",
            "detail": "",
            "review_status": "ok",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["event_date"] == "2025-01-12" and body["kind"] == "cambio_dosis"
    assert body["medication"] == "Medicina A2" and body["detail"] is None
    assert body["review_status"] == "ok"
    assert body["source_entry_id"] == event.source_entry_id
    assert body["source_entry_date"] == "2025-01-10"
    audit = test_db.scalars(
        select(AuditLog).where(
            AuditLog.entity == "med_event",
            AuditLog.action == "update",
            AuditLog.entity_id == str(event.id),
        )
    ).one()
    assert audit.user_id == member.id
    assert audit.before["review_status"] == "pending_review"
    assert audit.after["review_status"] == "ok"
    test_db.expire_all()
    assert test_db.get(MedEvent, event.id).updated_by == member.id


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "pausa"},
        {"kind": None},
        {"medication": ""},
        {"medication": "   "},
        {"medication": "X" * 201},
        {"event_date": None},
        {"event_date": "2025-02-30"},
        {"review_status": "discarded"},
        {"patient_id": 2},
        {"source_entry_id": 1},
    ],
)
def test_edit_validation(auth_client, patients, pending, body):
    assert auth_client.patch(url(patients[0], f"/{pending[0].id}"), json=body).status_code == 422


def test_cross_patient_and_missing_are_404(auth_client, test_db, patients, pending):
    papa, mama = patients
    papas = pending[0]
    assert papas.patient_id == papa.id
    assert (
        auth_client.patch(url(mama, f"/{papas.id}"), json={"review_status": "ok"}).status_code
        == 404
    )
    assert auth_client.delete(url(mama, f"/{papas.id}")).status_code == 404
    assert auth_client.patch(url(papa, "/999999"), json={"review_status": "ok"}).status_code == 404
    assert auth_client.delete("/api/v1/patients/999999/meds/events/1").status_code == 404
    test_db.expire_all()
    assert test_db.get(MedEvent, papas.id).review_status == "pending_review"


def test_discard_is_audited_hard_delete(auth_client, test_db, patients, pending, member):
    papa = patients[0]
    event = pending[1]
    assert auth_client.delete(url(papa, f"/{event.id}")).status_code == 204
    test_db.expire_all()
    assert test_db.get(MedEvent, event.id) is None
    audit = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "med_event", AuditLog.action == "delete")
    ).one()
    assert audit.user_id == member.id
    assert audit.before["medication"] == "Medicina B"
    assert audit.before["source_index"] == 1
    assert audit.before["created_by"] == member.id  # self-contained
    assert auth_client.delete(url(papa, f"/{event.id}")).status_code == 404


def test_confirm_all(auth_client, test_db, patients, pending, member):
    papa, mama = patients
    papas = [e for e in pending if e.patient_id == papa.id]
    mamas = [e for e in pending if e.patient_id == mama.id]
    # One already confirmed by someone else; a foreign id and an unknown id
    # in the list are ignored.
    auth_client.patch(url(papa, f"/{papas[0].id}"), json={"review_status": "ok"})
    ids = [e.id for e in papas] + [mamas[0].id, 999999]
    resp = auth_client.post(url(papa, "/confirm"), json={"ids": ids})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"confirmed": len(papas) - 1}
    test_db.expire_all()
    assert {test_db.get(MedEvent, e.id).review_status for e in papas} == {"ok"}
    assert test_db.get(MedEvent, mamas[0].id).review_status == "pending_review"
    updates = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "med_event", AuditLog.action == "update")
    ).all()
    assert len(updates) == len(papas)
    assert auth_client.post(url(papa, "/confirm"), json={"ids": ids}).json() == {"confirmed": 0}


@pytest.mark.parametrize("body", [{}, {"ids": []}, {"ids": list(range(1, 502))}, {"ids": "x"}])
def test_confirm_validation(auth_client, patients, body):
    assert auth_client.post(url(patients[0], "/confirm"), json=body).status_code == 422


def test_confirm_unknown_patient(auth_client, patients):
    resp = auth_client.post("/api/v1/patients/999999/meds/events/confirm", json={"ids": [1]})
    assert resp.status_code == 404


# ── Listing: keyset pagination and the pending list (ticket #82) ─────────────


def _many(test_db, patient, days, pending_every=0):
    """One synthetic event per day in ``days`` (repeats = ties on the date);
    every ``pending_every``-th is pending. Returns them in insertion order."""
    rows = [
        MedEvent(
            patient_id=patient.id,
            event_date=day,
            kind="inicio",
            medication=f"Medicina {i}",
            review_status=("pending_review" if pending_every and i % pending_every == 0 else "ok"),
        )
        for i, day in enumerate(days)
    ]
    test_db.add_all(rows)
    test_db.commit()
    return rows


def _newest_first(rows):
    return [r.id for r in sorted(rows, key=lambda r: (r.event_date, r.id), reverse=True)]


def _read_all(client, patient, limit):
    pages, cursor = [], None
    while True:
        suffix = f"?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        resp = client.get(url(patient, suffix))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        pages.append([e["id"] for e in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


def test_events_paging_with_date_ties(auth_client, test_db, patients):
    papa, mama = patients
    # 3 events on each of 4 days (ties on event_date), inserted out of order.
    rows = _many(test_db, papa, [date(2025, m, 1) for m in (3, 1, 4, 2)] * 3)
    _many(test_db, mama, [date(2025, 5, 1)] * 2)
    expected = _newest_first(rows)

    pages = _read_all(auth_client, papa, 5)
    assert [len(p) for p in pages] == [5, 5, 2]
    assert [i for p in pages for i in p] == expected  # no row skipped or repeated

    # An exact multiple: the last full page says there is nothing more.
    pages = _read_all(auth_client, papa, 4)
    assert [len(p) for p in pages] == [4, 4, 4]
    assert [i for p in pages for i in p] == expected

    # The default page holds everything here, and no cursor.
    body = auth_client.get(url(papa)).json()
    assert [e["id"] for e in body["items"]] == expected and body["next_cursor"] is None


def test_events_cursor_is_stable_under_writes(auth_client, test_db, patients):
    papa = patients[0]
    rows = _many(test_db, papa, [date(2025, 1, d) for d in range(1, 7)])
    first = auth_client.get(url(papa, "?limit=3")).json()
    assert [e["id"] for e in first["items"]] == [r.id for r in rows[::-1][:3]]
    # A newer event arrives and an older one is discarded between pages.
    _many(test_db, papa, [date(2025, 2, 1)])
    test_db.delete(rows[0])
    test_db.commit()
    second = auth_client.get(url(papa, f"?limit=3&cursor={first['next_cursor']}")).json()
    assert [e["id"] for e in second["items"]] == [rows[2].id, rows[1].id]
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "cursor",
    ["x", "2025-01-01", "2025-13-01_5", "2025-01-01_x", "2025-01-01_0", "2025-01-01_" + "9" * 30],
)
def test_events_bad_cursor(auth_client, patients, cursor):
    assert auth_client.get(url(patients[0], f"?cursor={cursor}")).status_code == 422


@pytest.mark.parametrize("query", ["?limit=0", "?limit=501", "?review_status=ok"])
def test_events_bad_params(auth_client, patients, query):
    assert auth_client.get(url(patients[0], query)).status_code == 422


def test_pending_list_is_complete_beyond_a_page(auth_client, test_db, patients):
    papa, mama = patients
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(30)]
    rows = _many(test_db, papa, days, pending_every=2)  # 15 pending, spread over all dates
    _many(test_db, mama, [date(2024, 6, 1)], pending_every=1)
    expected = _newest_first([r for r in rows if r.review_status == "pending_review"])

    # The first page holds only some of them...
    page = auth_client.get(url(papa, "?limit=5")).json()
    assert page["next_cursor"]
    # ...the pending list has all of them, whatever limit/cursor say.
    body = auth_client.get(
        url(papa, f"?review_status=pending_review&limit=1&cursor={page['next_cursor']}")
    ).json()
    assert [e["id"] for e in body["items"]] == expected
    assert {e["review_status"] for e in body["items"]} == {"pending_review"}
    assert body["next_cursor"] is None

    # «Confirmar todos» over that list empties it.
    resp = auth_client.post(url(papa, "/confirm"), json={"ids": expected})
    assert resp.json() == {"confirmed": 15}
    body = auth_client.get(url(papa, "?review_status=pending_review")).json()
    assert body == {"items": [], "next_cursor": None}


def test_events_list_requires_auth(db_client, patients):
    papa = patients[0]
    assert db_client.get(url(papa)).status_code == 401
    assert db_client.get(url(papa, "?review_status=pending_review")).status_code == 401
    assert db_client.get(url(papa, "?cursor=2025-01-01_1")).status_code == 401


def test_events_pending_unknown_patient(auth_client, patients):
    resp = auth_client.get("/api/v1/patients/999999/meds/events?review_status=pending_review")
    assert resp.status_code == 404
