"""One-off «Desde» repair of medication-list versions (#135, synthetic data)."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditLog
from app.models.medication import MedListVersion


def _url(patient):
    return f"/api/v1/patients/{patient.id}/meds"


def _make_versions(auth_client, patient, quantities, effective_date=None):
    """One version per quantity of a single synthetic medication."""
    for n, quantity in enumerate(quantities):
        extra = {"effective_date": effective_date} if n == 0 and effective_date else {}
        body = auth_client.get(_url(patient)).json()
        items = [i for g in body["slots"] for i in g["items"]]
        item = {"name": "Medicina A", "slot": "desayuno", "quantity": quantity}
        if items:
            item["id"] = items[0]["id"]
        resp = auth_client.put(
            _url(patient), json={"expected_version": n, "items": [item], **extra}
        )
        assert resp.json()["saved"] is True


def _versions(db, patient):
    db.expire_all()
    return db.scalars(
        select(MedListVersion)
        .where(MedListVersion.patient_id == patient.id)
        .order_by(MedListVersion.version_no)
    ).all()


def _null_out(db, version):
    version.effective_date = None
    version.snapshot = {**version.snapshot, "effective_date": None}


def test_repair_script(auth_client, test_engine, test_db, patients, member, monkeypatch, capsys):
    from scripts import meds_versions as script

    papa, mama = patients
    _make_versions(auth_client, papa, ["1", "2", "3", "4"], effective_date="2026-09-23")
    _make_versions(auth_client, mama, ["1", "2"])  # no «Desde» anywhere
    papa_v = _versions(test_db, papa)
    # As in production before #135: v2-v3 lost «Desde»; v4 was fixed by hand
    # (column only, snapshot key still null).
    _null_out(test_db, papa_v[1])
    _null_out(test_db, papa_v[2])
    papa_v[3].effective_date = date(2026, 10, 10)
    papa_v[3].snapshot = {**papa_v[3].snapshot, "effective_date": None}
    test_db.commit()

    monkeypatch.setattr(script, "SessionLocal", sessionmaker(bind=test_engine))
    assert script.main(["repair-effective-date", "--by", "ana@example.com", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run, nothing written" in out
    assert "6 versions checked; would fill 2" in out
    assert "1 snapshot keys" in out and "2 left empty" in out
    assert "Medicina" not in out
    assert [v.effective_date for v in _versions(test_db, papa)][1:3] == [None, None]

    assert script.main(["repair-effective-date", "--by", "ana@example.com"]) == 0
    assert "filled 2" in capsys.readouterr().out
    papa_v = _versions(test_db, papa)
    assert [v.effective_date for v in papa_v] == [
        date(2026, 9, 23),
        date(2026, 9, 23),
        date(2026, 9, 23),
        date(2026, 10, 10),
    ]
    assert [v.snapshot["effective_date"] for v in papa_v] == [
        "2026-09-23",
        "2026-09-23",
        "2026-09-23",
        "2026-10-10",
    ]
    assert all(v.effective_date is None for v in _versions(test_db, mama))
    rows = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "med_list_version", AuditLog.action == "update")
    ).all()
    assert len(rows) == 3 and {r.user_id for r in rows} == {member.id}
    assert {r.after["effective_date"] for r in rows} == {"2026-09-23", "2026-10-10"}

    # Idempotent.
    assert script.main(["repair-effective-date", "--by", "ana@example.com"]) == 0
    out = capsys.readouterr().out
    assert "filled 0" in out and "0 snapshot keys" in out
    assert script.main(["repair-effective-date", "--by", "nadie@example.com"]) == 1
