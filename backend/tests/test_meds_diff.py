"""Pure diff / summary tests for medication list versions (synthetic data)."""

import pytest

from app.services.meds_diff import (
    LINKS_ONLY,
    NO_CHANGES,
    diff_snapshots,
    format_days,
    initial_summary,
    is_empty,
    summarize,
    summary_for,
)


def item(id_, name="Medicina A", **kw):
    base = {
        "id": id_,
        "name": name,
        "strength": None,
        "quantity": "1",
        "dose_note": None,
        "slot": "desayuno",
        "time": None,
        "days": None,
        "schedule_note": None,
        "instructions": None,
        "prescriber_id": None,
        "prescriber_name": None,
        "reason": None,
        "start_date": None,
        "end_date": None,
        "sort_order": 0,
        "source_entry_id": None,
    }
    base.update(kw)
    return base


def avoid(id_, substance, reason=None):
    return {"id": id_, "substance": substance, "reason": reason, "source_entry_id": None}


def snap(items=(), avoid_rows=()):
    return {"items": list(items), "avoid": list(avoid_rows)}


A = item(1, "Medicina A", strength="250 mg", quantity="1")
B = item(2, "Medicina B", quantity="2", slot="cena")
C = item(3, "Medicina C", slot="desayuno", sort_order=1)

CASES = [
    pytest.param(snap([A]), snap([A]), NO_CHANGES, id="identical"),
    pytest.param(
        snap([A]),
        snap([A, B]),
        "Se agregó Medicina B (2, Cena).",
        id="add",
    ),
    pytest.param(
        snap([A]),
        snap([A, item(9, "Medicina Z", quantity=None, slot="otros")]),
        "Se agregó Medicina Z (Otros medicamentos).",
        id="add-without-quantity",
    ),
    pytest.param(snap([A, B]), snap([B]), "Se suspendió Medicina A 250 mg.", id="remove"),
    pytest.param(
        snap([A]),
        snap([{**A, "quantity": "½"}]),
        "Medicina A cambió de 1 a ½.",
        id="fraction",
    ),
    pytest.param(
        snap([{**A, "quantity": "8.5–17 g"}]),
        snap([{**A, "quantity": "¼"}]),
        "Medicina A cambió de 8.5–17 g a ¼.",
        id="text-quantity",
    ),
    pytest.param(
        snap([A]),
        snap([{**A, "slot": "noche"}]),
        "Medicina A pasó de Desayuno a Noche.",
        id="slot-move",
    ),
    pytest.param(
        snap([A]),
        snap([{**A, "name": "Medicina A2"}]),
        "Se renombró Medicina A a Medicina A2.",
        id="rename",
    ),
    pytest.param(
        snap([A]),
        snap([{**A, "name": "Medicina A2", "quantity": "½"}]),
        "Se renombró Medicina A a Medicina A2; Medicina A2 cambió de 1 a ½.",
        id="rename-and-quantity",
    ),
    pytest.param(
        snap([A]),
        snap([{**A, "quantity": "½", "instructions": "con la cena", "days": [1, 3, 5]}]),
        "Medicina A: cantidad de 1 a ½, días de todos los días a lun, mié, vie, "
        "instrucciones de — a con la cena.",
        id="several-fields",
    ),
    pytest.param(
        snap([A]),
        snap([{**A, "time": "06:00", "start_date": "2026-01-05"}]),
        "Medicina A: hora de — a 06:00, fecha de inicio de — a 05/01/2026.",
        id="time-and-date",
    ),
    pytest.param(
        snap([A]),
        snap([{**A, "prescriber_id": 4, "prescriber_name": "Dr. Sintético"}]),
        "Medicina A: médico de — a Dr. Sintético.",
        id="prescriber",
    ),
    pytest.param(
        snap([{**A, "prescriber_id": 4, "prescriber_name": "Dr. Viejo"}]),
        snap([{**A, "prescriber_id": 4, "prescriber_name": "Dr. Nuevo"}]),
        NO_CHANGES,
        id="prescriber-name-only-is-display",
    ),
    pytest.param(
        snap([{**A, "instructions": None}]),
        snap([{**A, "instructions": "  "}]),
        NO_CHANGES,
        id="blank-equals-null",
    ),
    pytest.param(
        snap([A, C]),
        snap([{**C, "sort_order": 0}, {**A, "sort_order": 1}]),
        "Se reordenó Desayuno.",
        id="reorder",
    ),
    pytest.param(
        snap([A, C]),
        snap([{**A, "sort_order": 5}, {**C, "sort_order": 9}]),
        NO_CHANGES,
        id="renumber-only-is-no-change",
    ),
    pytest.param(
        snap([A, item(5, "Medicina E", sort_order=1), {**C, "sort_order": 2}]),
        snap([A, {**C, "sort_order": 1}]),
        "Se suspendió Medicina E.",
        id="removal-is-not-reorder",
    ),
    pytest.param(
        snap([A, C]),
        snap([{**C, "sort_order": 0}, {**A, "slot": "cena"}]),
        "Medicina A pasó de Desayuno a Cena.",
        id="slot-move-is-not-reorder",
    ),
    pytest.param(
        snap([A]),
        snap([A], [avoid(1, "Sustancia X")]),
        "Se agregó Sustancia X a EVITAR.",
        id="avoid-add",
    ),
    pytest.param(
        snap([A], [avoid(1, "Sustancia X")]),
        snap([A]),
        "Se quitó Sustancia X de EVITAR.",
        id="avoid-remove",
    ),
    pytest.param(
        snap([], [avoid(1, "Sustancia X")]),
        snap([], [avoid(1, "Sustancia X", "alergia")]),
        "Se modificó Sustancia X en EVITAR (motivo de — a alergia).",
        id="avoid-change",
    ),
    pytest.param(
        snap([A, B], [avoid(1, "Sustancia X")]),
        snap([{**B, "quantity": "½"}, C], [avoid(2, "Sustancia Y")]),
        "Se agregó Medicina C (1, Desayuno); se suspendió Medicina A 250 mg; "
        "Medicina B cambió de 2 a ½; se agregó Sustancia Y a EVITAR; "
        "se quitó Sustancia X de EVITAR.",
        id="combined",
    ),
    # List-level fields (#135).
    pytest.param(
        {**snap([A]), "effective_date": "2026-09-23", "list_note": "| a |"},
        {**snap([A]), "effective_date": "2026-10-10", "list_note": "| a |"},
        "«Desde» pasó de 23/09/2026 a 10/10/2026.",
        id="desde-changed",
    ),
    pytest.param(
        snap([A]),
        {**snap([A]), "effective_date": "2026-10-10"},
        "«Desde»: 10/10/2026.",
        id="desde-set",
    ),
    pytest.param(
        {**snap([A]), "effective_date": "2026-10-10"},
        {**snap([A]), "effective_date": None},
        "Se quitó la fecha «Desde».",
        id="desde-cleared",
    ),
    pytest.param(
        {**snap([A]), "list_note": None},
        {**snap([A]), "list_note": "| Hora |"},
        "Se agregó la nota de la lista.",
        id="note-added",
    ),
    pytest.param(
        {**snap([A]), "list_note": "| Hora |"},
        {**snap([A]), "list_note": "| Hora | Dosis |"},
        "Se modificó la nota de la lista.",
        id="note-changed",
    ),
    pytest.param(
        {**snap([A]), "list_note": "| Hora |"},
        {**snap([A]), "list_note": "  "},
        "Se quitó la nota de la lista.",
        id="note-removed",
    ),
    pytest.param(
        {**snap([A]), "list_note": "| Hora |"},
        {**snap([A]), "list_note": "| Hora |\n"},
        NO_CHANGES,
        id="note-whitespace-only",
    ),
    pytest.param(
        {**snap([A]), "effective_date": "2026-09-23"},
        {**snap([A, B]), "effective_date": "2026-10-10", "list_note": "| x |"},
        "Se agregó Medicina B (2, Cena); «Desde» pasó de 23/09/2026 a 10/10/2026; "
        "se agregó la nota de la lista.",
        id="items-and-list-fields",
    ),
]


def test_list_change_shape():
    diff = diff_snapshots({**snap([A]), "list_note": "| a |"}, {**snap([A]), "list_note": "| b |"})
    (change,) = diff["list_changed"]
    assert change["field"] == "list_note" and change["label"] == "nota de la lista"
    assert change["before"] == "| a |" and change["after"] == "| b |"
    assert change["before_text"] == change["after_text"] == "con nota"  # never the note itself
    assert not is_empty(diff)
    assert is_empty(diff_snapshots(snap([A]), snap([A])))  # missing keys read as None


@pytest.mark.parametrize(("before", "after", "expected"), CASES)
def test_summary(before, after, expected):
    assert summarize(diff_snapshots(before, after)) == expected


def test_source_entry_change_is_in_diff_but_not_in_summary():
    # A bitácora hard delete nulls source_entry_id outside any save.
    linked = {**avoid(1, "Sustancia X"), "source_entry_id": 12}
    before = snap([{**A, "source_entry_id": 12}], [linked])
    after = snap([A], [avoid(1, "Sustancia X")])
    diff = diff_snapshots(before, after)
    assert diff["changed"][0]["fields"][0]["field"] == "source_entry_id"
    assert diff["avoid_changed"][0]["fields"][0]["field"] == "source_entry_id"
    assert summarize(diff) == LINKS_ONLY


def test_source_entry_change_does_not_hide_other_changes():
    before = snap([{**A, "source_entry_id": 12}])
    after = snap([{**A, "quantity": "½"}])
    assert summarize(diff_snapshots(before, after)) == "Medicina A cambió de 1 a ½."


def test_prescriber_repointed_by_merge_is_reported():
    before = snap([{**A, "prescriber_id": 4, "prescriber_name": "Dra. Duplicada"}])
    after = snap([{**A, "prescriber_id": 5, "prescriber_name": "Dra. Conservada"}])
    assert summarize(diff_snapshots(before, after)) == (
        "Medicina A: médico de Dra. Duplicada a Dra. Conservada."
    )


def test_identity_is_by_id():
    diff = diff_snapshots(snap([A]), snap([{**A, "name": "Otra"}]))
    assert diff["added"] == [] and diff["removed"] == []
    (change,) = diff["changed"]
    assert change["id"] == 1
    assert [f["field"] for f in change["fields"]] == ["name"]


def test_field_change_shape():
    diff = diff_snapshots(snap([A]), snap([{**A, "quantity": "½", "slot": "cena"}]))
    fields = diff["changed"][0]["fields"]
    assert fields == [
        {
            "field": "quantity",
            "label": "cantidad",
            "before": "1",
            "after": "½",
            "before_text": "1",
            "after_text": "½",
        },
        {
            "field": "slot",
            "label": "horario",
            "before": "desayuno",
            "after": "cena",
            "before_text": "Desayuno",
            "after_text": "Cena",
        },
    ]


def test_sort_order_is_never_a_field_change():
    diff = diff_snapshots(snap([A]), snap([{**A, "sort_order": 7}]))
    assert is_empty(diff)


def test_first_version_diff_is_all_added():
    after = snap([A, B], [avoid(1, "Sustancia X")])
    diff = diff_snapshots(None, after)
    assert [i["id"] for i in diff["added"]] == [1, 2]
    assert diff["avoid_added"] == [avoid(1, "Sustancia X")]
    assert summary_for(None, after) == "Lista inicial: 2 medicamentos y 1 sustancia en EVITAR."


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (snap([A]), "Lista inicial: 1 medicamento."),
        (
            snap([], [avoid(1, "X"), avoid(2, "Y")]),
            "Lista inicial: 0 medicamentos y 2 sustancias en EVITAR.",
        ),
    ],
)
def test_initial_summary(snapshot, expected):
    assert initial_summary(snapshot) == expected


def test_added_items_follow_sheet_order():
    after = snap([B, A, C])
    assert [i["id"] for i in diff_snapshots(snap(), after)["added"]] == [1, 3, 2]


@pytest.mark.parametrize(
    ("days", "text"),
    [(None, "todos los días"), ([], "todos los días"), ([6, 7], "sáb, dom"), ([2], "mar")],
)
def test_format_days(days, text):
    assert format_days(days) == text
