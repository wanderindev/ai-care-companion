"""LLM triage of the analytes under review (#100). Synthetic data, fake client."""

import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.main import app
from app.models.audit import AuditLog
from app.models.job import Job
from app.models.lab import Analyte, LabReport, LabResult
from app.services import jobs
from app.services.labs import triage

ANALYTES = "/api/v1/analytes"
SETTINGS = Settings(anthropic_api_key="test-key", extraction_model="claude-opus-5-5")


class FakeClient:
    """``beta.messages.create`` answers from ``decide(analyte_id) -> dict``."""

    def __init__(self, decide, stop_reason="end_turn"):
        self.decide = decide
        self.stop_reason = stop_reason
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **params):
        self.calls.append(params)
        batch = params["messages"][0]["content"][1]["text"]
        ids = [int(i) for i in re.findall(r"\[id (\d+)\]", batch)]
        proposals = []
        for analyte_id in ids:
            answer = self.decide(analyte_id)
            if answer is not None:
                proposals.append(
                    {
                        "analyte_id": analyte_id,
                        "target_id": 0,
                        "display_name": "",
                        "reason_code": "new_test",
                        **answer,
                    }
                )
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=json.dumps({"proposals": proposals}))],
        )


@pytest.fixture
def admin_client(auth_client, member, test_db):
    member.role = "admin"
    test_db.commit()
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    return auth_client


@pytest.fixture
def cat(test_db, patients):
    """Reviewed: Glucosa (mg/dL), Hemoglobina (g/dL), Glucosa en orina.
    Under review: Glicemia (mg/dL), HGB (%), Ferritina sérica, Nota del lab,
    Glucosa orina (orina)."""
    papa, _ = patients
    glu = Analyte(canonical_name="Glucosa", specimen="sangre", default_unit="mg/dL")
    hgb = Analyte(canonical_name="Hemoglobina", specimen="sangre", default_unit="g/dL")
    uglu = Analyte(canonical_name="Glucosa en orina", specimen="orina")
    glicemia = Analyte(canonical_name="Glicemia", specimen="sangre", needs_review=True)
    hgb_pct = Analyte(canonical_name="HGB", specimen="sangre", needs_review=True)
    ferr = Analyte(canonical_name="ferritina serica", specimen="sangre", needs_review=True)
    note = Analyte(canonical_name="Nota del lab", specimen="otro", needs_review=True)
    uglu2 = Analyte(canonical_name="Glucosa orina", specimen="orina", needs_review=True)
    test_db.add_all([glu, hgb, uglu, glicemia, hgb_pct, ferr, note, uglu2])
    test_db.flush()
    report = LabReport(
        patient_id=papa.id, source="upload", status="ok", lab_name="Laboratorio Sintético"
    )
    test_db.add(report)
    test_db.flush()
    for i, (analyte, value, unit) in enumerate(
        [
            (glicemia, "98", "mg/dL"),
            (glicemia, "101", "mg / dL"),
            (hgb_pct, "40", "%"),
            (ferr, "120", "ng/mL"),
            (glu, "90", "mg/dL"),
        ]
    ):
        test_db.add(
            LabResult(
                report_id=report.id,
                position=i,
                raw_name=analyte.canonical_name.upper(),
                value_text=value,
                unit=unit,
                analyte_id=analyte.id,
            )
        )
    test_db.commit()
    return SimpleNamespace(
        glu=glu, hgb=hgb, uglu=uglu, glicemia=glicemia, hgb_pct=hgb_pct, ferr=ferr, note=note,
        uglu2=uglu2, report=report,
    )  # fmt: skip


def decider(cat, overrides=None):
    table = {
        cat.glicemia.id: {
            "action": "merge_into",
            "target_id": cat.glu.id,
            "confidence": "high",
            "reason_code": "same_test_synonym",
        },
        cat.hgb_pct.id: {"action": "merge_into", "target_id": cat.hgb.id, "confidence": "high"},
        cat.ferr.id: {
            "action": "keep_as_new",
            "display_name": "Ferritina sérica",
            "confidence": "high",
        },
        cat.note.id: {"action": "unsure", "confidence": "low", "reason_code": "not_a_test"},
        cat.uglu2.id: {"action": "merge_into", "target_id": cat.uglu.id, "confidence": "medium"},
    }
    table.update(overrides or {})
    return lambda analyte_id: table.get(analyte_id)


def _fresh(db, analyte_id):
    db.expire_all()
    return db.get(Analyte, analyte_id)


def test_request_carries_names_units_and_examples_only(test_db, cat, patients):
    client = FakeClient(decider(cat))
    triage.run_triage(test_db, SETTINGS, client=client)
    assert len(client.calls) == 1
    params = client.calls[0]
    catalogue, batch = (b["text"] for b in params["messages"][0]["content"])
    assert params["model"] == "claude-opus-5-5"
    assert params["output_config"]["format"]["schema"] == triage.SPEC.schema
    assert f"[id {cat.glu.id}] Glucosa" in catalogue
    assert "Glicemia" not in catalogue  # only reviewed analytes are targets
    assert f"[id {cat.glicemia.id}] Glicemia" in batch
    assert "GLICEMIA = 98 mg/dL" in batch
    units = batch.split("unidades: ")[1].split(" |")[0]
    assert units in ("mg/dL", "mg / dL")  # spacing variants count as one unit
    text = json.dumps(params, ensure_ascii=False)
    for secret in ("Papá", "Laboratorio Sintético"):
        assert secret not in text


def test_proposals_only_without_apply(test_db, cat):
    stats = triage.run_triage(test_db, SETTINGS, client=FakeClient(decider(cat)))
    assert stats.proposals == {"merge_into": 3, "keep_as_new": 1, "unsure": 1}
    assert sum(stats.auto_applied.values()) == 0
    glicemia = _fresh(test_db, cat.glicemia.id)
    assert glicemia.needs_review is True
    assert glicemia.triage["action"] == "merge_into"
    assert glicemia.triage["target_id"] == cat.glu.id
    assert glicemia.triage["units_match"] is True
    assert glicemia.triage["status"] == "pending"
    assert glicemia.triage["version"] == "analyte_triage_v1"
    assert _fresh(test_db, cat.hgb_pct.id).triage["units_match"] is False
    assert test_db.scalar(select(AuditLog)) is None

    again = FakeClient(decider(cat))
    assert triage.run_triage(test_db, SETTINGS, client=again).candidates == 0
    assert again.calls == []  # a triaged analyte is never sent again


def test_apply_only_the_safe_cases(test_db, cat):
    stats = triage.run_triage(test_db, SETTINGS, client=FakeClient(decider(cat)), apply=True)
    assert stats.auto_applied == {"merged": 1, "kept": 1}
    # high-confidence merge, same specimen, units match → merged
    assert _fresh(test_db, cat.glicemia.id) is None
    glu = _fresh(test_db, cat.glu.id)
    assert "Glicemia" in glu.aliases
    moved = test_db.scalars(select(LabResult).where(LabResult.raw_name == "GLICEMIA")).all()
    assert {r.analyte_id for r in moved} == {glu.id}
    # high-confidence keep → confirmed with the cleaned name, old name an alias
    ferr = _fresh(test_db, cat.ferr.id)
    assert (ferr.needs_review, ferr.canonical_name) == (False, "Ferritina sérica")
    assert ferr.triage["status"] == "applied"
    # units differ (% vs g/dL), medium confidence, unsure → proposals
    for analyte_id in (cat.hgb_pct.id, cat.uglu2.id, cat.note.id):
        analyte = _fresh(test_db, analyte_id)
        assert analyte.needs_review and analyte.triage["status"] == "pending"
    actions = {a.action for a in test_db.scalars(select(AuditLog))}
    assert actions == {"merge", "delete", "update"}


def test_merge_of_an_analyte_without_units_is_never_auto_applied(test_db, cat):
    """No unit on the source: nothing shows the two measure the same, so even
    a high-confidence, same-specimen merge stays a proposal (#113)."""
    high = {"action": "merge_into", "target_id": cat.uglu.id, "confidence": "high"}
    decide = decider(cat, {cat.uglu2.id: high})
    stats = triage.run_triage(test_db, SETTINGS, client=FakeClient(decide), apply=True)
    assert stats.auto_applied == {"merged": 1, "kept": 1}  # Glicemia and Ferritina only
    uglu2 = _fresh(test_db, cat.uglu2.id)
    assert uglu2.needs_review is True
    assert uglu2.triage["status"] == "pending"
    assert uglu2.triage["units_match"] is None
    assert triage.units_match(test_db, uglu2, _fresh(test_db, cat.uglu.id)) is None
    # an admin can still accept it by hand
    triage.apply_proposal(test_db, uglu2, user_id=None)
    test_db.commit()
    assert _fresh(test_db, cat.uglu2.id) is None


def test_dry_run_saves_nothing(test_db, cat):
    stats = triage.run_triage(
        test_db, SETTINGS, client=FakeClient(decider(cat)), apply=True, dry_run=True
    )
    assert stats.auto_applied == {"merged": 1, "kept": 1}
    assert all(a.triage is None for a in test_db.scalars(select(Analyte)))
    assert _fresh(test_db, cat.glicemia.id) is not None


def test_invalid_target_and_bad_answers(test_db, cat):
    decide = decider(
        cat,
        {
            cat.glicemia.id: {"action": "merge_into", "target_id": 9999, "confidence": "high"},
            cat.ferr.id: {"action": "merge_into", "target_id": cat.note.id, "confidence": "high"},
        },
    )
    triage.run_triage(test_db, SETTINGS, client=FakeClient(decide), apply=True)
    for analyte_id in (cat.glicemia.id, cat.ferr.id):  # unknown / not reviewed target
        analyte = _fresh(test_db, analyte_id)
        assert analyte.triage["action"] == "unsure"
        assert analyte.triage["reason_code"] == "invalid_target"

    test_db.query(Analyte).update({Analyte.triage: None})
    test_db.commit()
    stats = triage.run_triage(test_db, SETTINGS, client=FakeClient(decide, "max_tokens"))
    assert stats.errors == {"truncated": 1}
    assert all(a.triage is None for a in test_db.scalars(select(Analyte)))


def test_batches_and_estimate(test_db, cat, monkeypatch):
    monkeypatch.setattr(triage, "BATCH_SIZE", 2)
    est = triage.estimate(test_db, SETTINGS)
    assert (est.analytes, est.batches) == (5, 3)
    assert est.cost_usd is not None and est.cost_usd > 0
    client = FakeClient(decider(cat))
    stats = triage.run_triage(test_db, SETTINGS, client=client)
    assert (len(client.calls), stats.batches, stats.usage.requests) == (3, 3, 3)
    assert triage.estimate(test_db, SETTINGS).analytes == 0


def test_missing_key_is_permanent(test_db, cat):
    with pytest.raises(jobs.PermanentJobError):
        triage.run_triage(test_db, Settings(anthropic_api_key=""))


# ── API ─────────────────────────────────────────────────────────────────────


def test_review_list_shows_proposals_and_accept(admin_client, test_db, cat):
    triage.run_triage(test_db, SETTINGS, client=FakeClient(decider(cat)))
    items = {a["id"]: a for a in admin_client.get(ANALYTES, params={"needs_review": "true"}).json()}
    assert items[cat.glicemia.id]["triage"]["target_name"] == "Glucosa"
    assert items[cat.ferr.id]["triage"]["display_name"] == "Ferritina sérica"

    resp = admin_client.post(f"{ANALYTES}/{cat.glicemia.id}/triage/accept")
    assert resp.status_code == 200
    assert resp.json()["id"] == cat.glu.id
    assert _fresh(test_db, cat.glicemia.id) is None

    resp = admin_client.post(f"{ANALYTES}/{cat.ferr.id}/triage/accept")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["canonical_name"], body["needs_review"], body["triage"]) == (
        "Ferritina sérica",
        False,
        None,
    )
    assert admin_client.post(f"{ANALYTES}/{cat.ferr.id}/triage/accept").status_code == 409
    assert admin_client.post(f"{ANALYTES}/{cat.note.id}/triage/accept").status_code == 409


def test_reject_and_accept_all(admin_client, test_db, cat):
    decide = decider(
        cat,  # a cross-specimen merge: the specimen rule refuses it
        {cat.uglu2.id: {"action": "merge_into", "target_id": cat.glu.id, "confidence": "high"}},
    )
    triage.run_triage(test_db, SETTINGS, client=FakeClient(decide))

    resp = admin_client.post(f"{ANALYTES}/{cat.hgb_pct.id}/triage/reject")
    assert resp.status_code == 200
    assert resp.json()["triage"]["status"] == "rejected"
    entry = test_db.scalar(
        select(AuditLog).where(
            AuditLog.entity == "analyte", AuditLog.entity_id == str(cat.hgb_pct.id)
        )
    )
    assert entry.after["triage"]["status"] == "rejected"

    assert admin_client.get(f"{ANALYTES}/triage/estimate").json()["pending_proposals"] == 3
    resp = admin_client.post(f"{ANALYTES}/triage/accept-all")
    assert resp.json() == {"accepted": 2, "failed": 1}
    assert _fresh(test_db, cat.glicemia.id) is None
    assert _fresh(test_db, cat.ferr.id).needs_review is False
    uglu2 = _fresh(test_db, cat.uglu2.id)
    assert uglu2.triage["status"] == "pending" and uglu2.triage["error"] == "refused_409"
    assert _fresh(test_db, cat.hgb_pct.id).needs_review is True  # rejected: untouched


def test_writes_are_admin_only(auth_client, test_db, cat):
    assert auth_client.get(f"{ANALYTES}/triage/estimate").status_code == 403
    assert auth_client.post(f"{ANALYTES}/triage").status_code == 403
    assert auth_client.post(f"{ANALYTES}/triage/accept-all").status_code == 403
    assert auth_client.post(f"{ANALYTES}/{cat.ferr.id}/triage/accept").status_code == 403
    assert auth_client.post(f"{ANALYTES}/{cat.ferr.id}/triage/reject").status_code == 403


def test_start_from_page_runs_the_job(admin_client, test_db, test_engine, cat, monkeypatch):
    est = admin_client.get(f"{ANALYTES}/triage/estimate").json()
    assert (est["analytes"], est["batches"], est["running_job_id"]) == (5, 1, None)

    resp = admin_client.post(f"{ANALYTES}/triage")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert admin_client.post(f"{ANALYTES}/triage").status_code == 409  # one at a time
    assert admin_client.get(f"{ANALYTES}/triage/estimate").json()["running_job_id"] == job_id

    from app.services.labs import jobs as lab_jobs  # registers the handler

    monkeypatch.setattr(lab_jobs, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(triage, "make_client", lambda key: FakeClient(decider(cat)))
    factory = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    assert jobs.run_next(factory) is True
    job = test_db.get(Job, job_id)
    test_db.refresh(job)
    assert (job.status, job.progress) == ("done", 100)
    # proposals only: nothing applied from the page
    assert _fresh(test_db, cat.ferr.id).triage["status"] == "pending"
    assert _fresh(test_db, cat.glicemia.id) is not None
    assert admin_client.post(f"{ANALYTES}/triage").status_code == 409  # nothing left


def test_start_without_key(admin_client, cat):
    app.dependency_overrides[get_settings] = lambda: Settings(anthropic_api_key="")
    assert admin_client.post(f"{ANALYTES}/triage").status_code == 503


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_prints_estimate_and_counts(test_db, test_engine, cat, monkeypatch, capsys):
    from scripts import analytes as cli

    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=test_engine))
    monkeypatch.setattr(cli, "get_settings", lambda: SETTINGS)
    args = SimpleNamespace(dry_run=False, apply=True, yes=False)

    assert cli.triage_command(args, client=FakeClient(decider(cat)), confirm=lambda _: "n") == 1
    assert "To triage: 5 analytes in 1 call(s)" in capsys.readouterr().out

    assert cli.triage_command(args, client=FakeClient(decider(cat)), confirm=lambda _: "y") == 0
    out = capsys.readouterr().out
    assert "Applied: 2 (kept 1, merged 1)" in out
    assert "Claude calls: 1 (input 100, output 50" in out
    for name in ("Glicemia", "Ferritina", "98"):
        assert name not in out


def _api_error(status: int):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "error", response=httpx2.Response(status, request=request), body=None
    )


def test_refused_request_is_not_counted(test_db, cat, monkeypatch):
    """A 400 bills nothing: it counts as an error, not as a request (#113)."""
    monkeypatch.setattr(triage, "BATCH_SIZE", 2)
    client = FakeClient(decider(cat))
    answer = client.create

    def create(**params):
        if len(client.calls) == 1:  # the second batch
            client.calls.append(params)
            raise _api_error(400)
        return answer(**params)

    client.beta.messages.create = create
    stats = triage.run_triage(test_db, SETTINGS, client=client)
    assert (len(client.calls), stats.batches) == (3, 3)
    assert stats.errors == {"api_error_400": 1}
    assert stats.usage.as_dict() == {
        "requests": 2,
        "input_tokens": 200,
        "output_tokens": 100,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


@pytest.mark.parametrize("module", ["triage", "daily_analysis"])
def test_request_worst_case_fits_job_lock(module):
    """Progress is reported only before each call: one call with all its SDK
    retries must end before stuck-job recovery would rerun the job (#113)."""
    from app.services.daily_analysis import analyze

    mod = triage if module == "triage" else analyze
    backoff = 10 * mod.MAX_RETRIES  # the SDK's backoff is at most 8 s a retry
    worst = mod.REQUEST_TIMEOUT_SECONDS * (1 + mod.MAX_RETRIES) + backoff
    assert worst < jobs.LOCK_TIMEOUT.total_seconds() - 60
