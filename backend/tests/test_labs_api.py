"""Labs screens API (#14): report list/detail/edit and analyte series.

Synthetic data only: made-up labs, values and dates.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.lab import Analyte, LabReport, LabResult

URL = "/api/v1/lab-reports"


def _report(db, patient, when, *, results=(), status="ok", reported=None, lab="Lab Uno", **kw):
    report = LabReport(
        patient_id=patient.id if patient else None,
        collected_on=when,
        reported_on=reported,
        lab_name=lab,
        source="whatsapp",
        status=status,
        patient_match="cip" if patient else None,
        **kw,
    )
    db.add(report)
    db.flush()
    for i, spec in enumerate(results):
        db.add(LabResult(report_id=report.id, position=i, **spec))
    db.commit()
    return report


def _res(name, value, *, analyte=None, flag=None, unit="mg/dL", low=None, high=None, **kw):
    return {
        "raw_name": name,
        "analyte_id": analyte.id if analyte else None,
        "value_text": str(value) if value is not None else kw.pop("text", None),
        "value_num": Decimal(str(value)) if isinstance(value, (int, float)) else None,
        "unit": unit,
        "ref_low": Decimal(str(low)) if low is not None else None,
        "ref_high": Decimal(str(high)) if high is not None else None,
        "flag": flag,
        **kw,
    }


@pytest.fixture
def analytes(test_db):
    rows = [
        Analyte(canonical_name="Glucosa", aliases=["GLU", "Glicemia"], specimen="sangre"),
        Analyte(canonical_name="Vitamina B12", aliases=["Cobalamina"], specimen="sangre"),
        Analyte(canonical_name="Hemoglobina", aliases=["HGB"], specimen="sangre"),
    ]
    test_db.add_all(rows)
    test_db.commit()
    return {a.canonical_name: a for a in rows}


@pytest.fixture
def labs(test_db, patients, analytes):
    papa, mama = patients
    glu, b12, hgb = analytes["Glucosa"], analytes["Vitamina B12"], analytes["Hemoglobina"]
    return {
        "p_old": _report(
            test_db,
            papa,
            date(2025, 1, 10),
            results=[_res("GLUCOSA", 95, analyte=glu, flag="normal", low=70, high=100)],
        ),
        "p_new": _report(
            test_db,
            papa,
            date(2025, 6, 2),
            results=[
                _res("Glucosa en ayunas", 130, analyte=glu, flag="alto", low=70, high=100),
                _res("Cobalamína sérica", 300, analyte=b12, flag="normal", unit="pg/mL"),
            ],
        ),
        "m_mid": _report(
            test_db,
            mama,
            date(2025, 3, 15),
            results=[
                _res(
                    "HGB",
                    10.2,
                    analyte=hgb,
                    flag="bajo",
                    unit="g/dL",
                    low=12,
                    high=16,
                    panel="Hemograma",
                )
            ],
        ),
        # Reported only (no collection date) and with no patient identified.
        "unknown": _report(
            test_db,
            None,
            None,
            reported=date(2025, 4, 1),
            status="needs_review",
            results=[_res("Glucosa", 88, analyte=glu, flag="normal")],
        ),
    }


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


def test_requires_sign_in(db_client):
    assert db_client.get(URL).status_code == 401
    assert db_client.get(f"{URL}/1").status_code == 401
    assert db_client.get("/api/v1/analytes/1/series?patient=1").status_code == 401
    headers = {"X-Requested-With": "fetch"}
    assert db_client.patch(f"{URL}/1", json={}, headers=headers).status_code == 401


def test_write_needs_csrf_header(auth_client, labs):
    del auth_client.headers["X-Requested-With"]
    resp = auth_client.patch(f"{URL}/{labs['p_new'].id}", json={"lab_name": "X"})
    assert resp.status_code == 403


def test_list_order_and_summary(auth_client, labs):
    resp = auth_client.get(URL)
    # Report date: collected, else reported (the unidentified report: 1 Apr).
    assert _ids(resp) == [labs[k].id for k in ("p_new", "unknown", "m_mid", "p_old")]
    body = resp.json()
    assert body["total"] == 4
    first = body["items"][0]
    assert first["report_date"] == "2025-06-02"
    assert first["result_count"] == 2
    assert first["out_of_range_count"] == 1
    assert first["flagged_names"] == ["Glucosa en ayunas"]
    unknown = body["items"][1]
    assert (unknown["patient_id"], unknown["report_date"]) == (None, "2025-04-01")
    assert body["items"][2]["panels"] == ["Hemograma"]

    page = auth_client.get(f"{URL}?limit=2&offset=2").json()
    assert [i["id"] for i in page["items"]] == [labs["m_mid"].id, labs["p_old"].id]
    assert page["total"] == 4


def test_list_falls_back_to_created_date(auth_client, test_db, patients):
    papa = patients[0]
    undated = _report(test_db, papa, None)
    dated = _report(test_db, papa, date(2020, 1, 1))
    items = auth_client.get(URL).json()["items"]
    assert [i["id"] for i in items] == [undated.id, dated.id]
    assert items[0]["report_date"] == undated.created_at.date().isoformat()


def test_patient_scoping_and_unassigned(auth_client, patients, labs):
    papa, mama = patients
    assert _ids(auth_client.get(f"{URL}?patient_id={papa.id}")) == [
        labs["p_new"].id,
        labs["p_old"].id,
    ]
    with_unknown = auth_client.get(f"{URL}?patient_id={mama.id}&include_unassigned=true")
    assert _ids(with_unknown) == [labs["unknown"].id, labs["m_mid"].id]
    both = auth_client.get(f"{URL}?patient_id={papa.id}&patient_id={mama.id}")
    assert labs["unknown"].id not in _ids(both)
    assert len(_ids(both)) == 3


def test_filters(auth_client, patients, labs):
    review = auth_client.get(f"{URL}?status=needs_review")
    assert _ids(review) == [labs["unknown"].id]
    out = auth_client.get(f"{URL}?out_of_range=true")
    assert _ids(out) == [labs["p_new"].id, labs["m_mid"].id]
    dates = auth_client.get(f"{URL}?date_from=2025-03-15&date_to=2025-04-01")
    assert _ids(dates) == [labs["unknown"].id, labs["m_mid"].id]


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("glucosa", ["p_new", "unknown", "p_old"]),  # raw names and the analyte
        ("GLICEMIA", ["p_new", "unknown", "p_old"]),  # an alias
        ("cobalamina", ["p_new"]),  # raw name with an accent, alias without
        ("vitamina b12", ["p_new"]),  # canonical name, not printed on the report
        ("hemoglobina", ["m_mid"]),
        ("ayunas", ["p_new"]),
        ("100%", []),  # LIKE wildcards are literal
        ("zzz", []),
    ],
)
def test_test_name_search(auth_client, labs, q, expected):
    assert _ids(auth_client.get(URL, params={"q": q})) == [labs[k].id for k in expected]


def test_search_and_out_of_range_need_the_same_result(auth_client, labs):
    # p_new has an out-of-range Glucosa but a normal B12.
    assert _ids(auth_client.get(URL, params={"q": "glucosa", "out_of_range": "true"})) == [
        labs["p_new"].id
    ]
    assert _ids(auth_client.get(URL, params={"q": "b12", "out_of_range": "true"})) == []


def test_detail(auth_client, test_db, labs, storage):
    attachment = Attachment(
        kind="document",
        filename="informe.pdf",
        mime="application/pdf",
        size=1234,
        sha256="a" * 64,
        storage_key="lab/2025/" + "a" * 64 + ".pdf",
        owner_type="lab_report",
        owner_id=labs["p_new"].id,
    )
    test_db.add(attachment)
    test_db.flush()
    labs["p_new"].attachment_id = attachment.id
    test_db.commit()

    body = auth_client.get(f"{URL}/{labs['p_new'].id}").json()
    assert body["attachment"]["filename"] == "informe.pdf"
    first, second = body["results"]
    assert first["analyte_name"] == "Glucosa"
    assert (first["value_num"], first["ref_low"], first["ref_high"]) == (130, 70, 100)
    assert first["flag"] == "alto"
    assert second["raw_name"] == "Cobalamína sérica"
    assert "raw_extraction" not in body
    assert auth_client.get(f"{URL}/999999").status_code == 404


def test_patch_assigns_patient_and_marks_reviewed(auth_client, test_db, patients, labs, member):
    report = labs["unknown"]
    url = f"{URL}/{report.id}"
    # Reviewed needs a patient.
    resp = auth_client.patch(url, json={"status": "ok"})
    assert resp.status_code == 400
    assert "paciente" in resp.json()["detail"]

    mama = patients[1]
    resp = auth_client.patch(
        url, json={"patient_id": mama.id, "status": "ok", "collected_on": "2025-03-30"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["patient_id"], body["status"], body["patient_match"]) == (mama.id, "ok", "manual")
    assert body["report_date"] == "2025-03-30"

    row = test_db.scalars(select(AuditLog).where(AuditLog.entity == "lab_report")).one()
    assert (row.action, row.user_id, row.entity_id) == ("update", member.id, str(report.id))
    assert row.before["patient_id"] is None and row.before["status"] == "needs_review"
    assert row.after["patient_id"] == mama.id and row.after["patient_match"] == "manual"
    assert "raw_extraction" in row.after  # the snapshot is the whole row
    test_db.refresh(report)
    assert report.updated_by == member.id

    # A no-op patch adds no audit row.
    auth_client.patch(url, json={"status": "ok"})
    assert len(test_db.scalars(select(AuditLog).where(AuditLog.entity == "lab_report")).all()) == 1


def test_patch_lab_name_and_dates(auth_client, labs):
    url = f"{URL}/{labs['p_old'].id}"
    resp = auth_client.patch(url, json={"lab_name": "  Lab Dos ", "reported_on": "2025-01-12"})
    assert resp.status_code == 200
    assert (resp.json()["lab_name"], resp.json()["reported_on"]) == ("Lab Dos", "2025-01-12")
    assert auth_client.patch(url, json={"lab_name": "  "}).json()["lab_name"] is None
    assert auth_client.patch(url, json={"status": "needs_review"}).json()["status"] == (
        "needs_review"
    )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"patient_id": 999}, 400),
        ({"patient_id": None}, 422),
        ({"status": "failed"}, 422),
        ({"status": None}, 422),
        ({"collected_on": "1800-01-01"}, 400),
        ({"collected_on": (date.today() + timedelta(days=5)).isoformat()}, 400),
        ({"lab_name": "x" * 201}, 422),
        ({"raw_extraction": {}}, 200),  # unknown fields are ignored, never written
    ],
)
def test_patch_validation(auth_client, test_db, labs, payload, code):
    report = labs["p_new"]
    resp = auth_client.patch(f"{URL}/{report.id}", json=payload)
    assert resp.status_code == code, resp.text
    test_db.refresh(report)
    assert report.patient_id is not None
    assert report.raw_extraction is None
    assert auth_client.patch(f"{URL}/999999", json={}).status_code == 404


def test_series(auth_client, test_db, patients, analytes, labs):
    papa, mama = patients
    glu = analytes["Glucosa"]
    # A later report with a changed unit and range, and a non-numeric value.
    _report(
        test_db,
        papa,
        date(2025, 9, 1),
        lab="Lab Dos",
        results=[
            _res("Glucosa", 5.1, analyte=glu, unit="mmol/L", low=3.9, high=5.5, flag="normal"),
            _res("Glucosa (2)", None, analyte=glu, text="Hemolizada", unit=None),
        ],
    )
    _report(
        test_db,
        papa,
        date(2024, 12, 1),
        results=[_res("Glucosa", 60, analyte=glu, comparator="<", unit="mg/dL", high=100)],
    )
    resp = auth_client.get(f"/api/v1/analytes/{glu.id}/series", params={"patient": papa.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analyte"]["canonical_name"] == "Glucosa"
    points = body["points"]
    assert [p["date"] for p in points] == [
        "2024-12-01",
        "2025-01-10",
        "2025-06-02",
        "2025-09-01",
        "2025-09-01",
    ]
    first = points[0]
    assert (first["comparator"], first["value_num"], first["ref_low"], first["ref_high"]) == (
        "<",
        60,
        None,
        100,
    )
    assert points[2]["flag"] == "alto" and points[2]["report_id"] == labs["p_new"].id
    assert (points[3]["unit"], points[3]["ref_high"], points[3]["lab_name"]) == (
        "mmol/L",
        5.5,
        "Lab Dos",
    )
    assert (points[4]["value_num"], points[4]["value_text"]) == (None, "Hemolizada")

    # Another patient's results and unidentified reports stay out.
    other = auth_client.get(f"/api/v1/analytes/{glu.id}/series", params={"patient": mama.id})
    assert other.json()["points"] == []


def test_series_errors(auth_client, patients, analytes):
    glu = analytes["Glucosa"]
    assert (
        auth_client.get(f"/api/v1/analytes/999/series?patient={patients[0].id}").status_code == 404
    )
    assert auth_client.get(f"/api/v1/analytes/{glu.id}/series?patient=999").status_code == 404
    assert auth_client.get(f"/api/v1/analytes/{glu.id}/series").status_code == 422


# ── delete (#115) ───────────────────────────────────────────────────────────


@pytest.fixture
def admin(auth_client, member, test_db):
    member.role = "admin"
    test_db.commit()
    return auth_client


def test_delete_needs_sign_in_and_an_admin(db_client, test_db, labs, member):
    report_id = labs["unknown"].id
    headers = {"X-Requested-With": "fetch"}
    assert db_client.delete(f"{URL}/{report_id}", headers=headers).status_code == 401


def test_delete_refused_to_members(auth_client, test_db, labs):
    report_id = labs["unknown"].id
    assert auth_client.delete(f"{URL}/{report_id}").status_code == 403
    test_db.expire_all()
    assert test_db.get(LabReport, report_id) is not None


def test_delete_missing_report(admin):
    assert admin.delete(f"{URL}/999999").status_code == 404


def _page(n: int, report_id: int) -> Attachment:
    return Attachment(
        kind="document",
        filename=f"pagina-{n}.jpg",
        mime="image/jpeg",
        size=100,
        sha256=str(n) * 64,
        storage_key=f"lab/2025/{str(n) * 64}.jpg",
        owner_type="lab_report",
        owner_id=report_id,
    )


def test_delete_report(admin, test_db, labs, analytes, member):
    report = labs["unknown"]
    report_id = report.id
    glu = analytes["Glucosa"]
    # Two analytes only this report used (still under review; one with a
    # pending triage proposal), one shared with another report, and a
    # reviewed one left with no result.
    only = Analyte(canonical_name="Marcador Raro", specimen="sangre", needs_review=True)
    triaged = Analyte(
        canonical_name="Otro Marcador",
        specimen="sangre",
        needs_review=True,
        triage={"status": "pending", "action": "merge_into", "target_id": glu.id},
    )
    shared = Analyte(canonical_name="Marcador Común", specimen="sangre", needs_review=True)
    reviewed = Analyte(canonical_name="Marcador Revisado", specimen="sangre")
    test_db.add_all([only, triaged, shared, reviewed])
    test_db.flush()
    for i, a in enumerate([only, triaged, shared, reviewed], start=1):
        test_db.add(LabResult(report_id=report_id, position=i, **_res("X", 5, analyte=a)))
    test_db.add(LabResult(report_id=labs["p_old"].id, position=9, **_res("Y", 1, analyte=shared)))
    pages = [_page(1, report_id), _page(2, report_id)]
    test_db.add_all(pages)
    test_db.flush()
    report.attachment_id = pages[0].id
    test_db.commit()
    page_ids = sorted(p.id for p in pages)
    only_id, triaged_id = only.id, triaged.id
    shared_id, reviewed_id = shared.id, reviewed.id

    resp = admin.delete(f"{URL}/{report_id}")
    assert resp.status_code == 204, resp.text
    test_db.expire_all()

    assert test_db.get(LabReport, report_id) is None
    assert test_db.scalars(select(LabResult).where(LabResult.report_id == report_id)).all() == []
    assert len(test_db.get(LabReport, labs["p_old"].id).results) == 2
    # Files unlinked, never deleted.
    for page_id in page_ids:
        row = test_db.get(Attachment, page_id)
        assert row is not None and (row.owner_type, row.owner_id) == (None, None)
    # Orphans under review are deleted; shared, reviewed and other analytes stay.
    assert test_db.get(Analyte, only_id) is None
    assert test_db.get(Analyte, triaged_id) is None
    assert test_db.get(Analyte, shared_id) is not None
    assert test_db.get(Analyte, reviewed_id) is not None
    assert test_db.get(Analyte, glu.id) is not None
    review = admin.get("/api/v1/analytes", params={"needs_review": "true"}).json()
    assert [a["id"] for a in review] == [shared_id]

    entry = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "lab_report", AuditLog.action == "delete")
    ).one()
    assert (entry.user_id, entry.entity_id, entry.after) == (member.id, str(report_id), None)
    summary = entry.before
    assert (summary["id"], summary["patient_id"]) == (report_id, None)
    assert (summary["reported_on"], summary["lab_name"]) == ("2025-04-01", "Lab Uno")
    assert summary["result_count"] == 5
    assert summary["unlinked_attachment_ids"] == page_ids
    assert summary["deleted_analyte_ids"] == sorted([only_id, triaged_id])
    # Ids and counts only: no results, values or analyte names.
    assert "results" not in summary and "raw_extraction" not in summary
    assert "Marcador" not in str(summary) and "Glucosa" not in str(summary)
    unlinks = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "attachment", AuditLog.action == "unlink")
    ).all()
    assert sorted(int(a.entity_id) for a in unlinks) == page_ids
    gone = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "analyte", AuditLog.action == "delete")
    ).all()
    assert sorted(int(a.entity_id) for a in gone) == summary["deleted_analyte_ids"]
    assert all(a.after == {"orphaned_by_lab_report": report_id} for a in gone)

    assert admin.get(f"{URL}/{report_id}").status_code == 404
    assert admin.delete(f"{URL}/{report_id}").status_code == 404
