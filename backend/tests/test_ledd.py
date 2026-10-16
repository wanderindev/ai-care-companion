"""LEDD computation (app.services.ledd): pure, table-driven, synthetic lists."""

import pytest

from app.services import ledd


def item(name, strength=None, quantity="1", slot="desayuno", id_=None, **kw):
    return {
        "id": id_ if id_ is not None else abs(hash((name, slot, strength, quantity))) % 10_000,
        "name": name,
        "strength": strength,
        "quantity": quantity,
        "slot": slot,
        **kw,
    }


def total(items, learned=None):
    return ledd.compute(items, learned)["total_mg"]


# ── Parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", 1.0),
        ("½", 0.5),
        ("¼", 0.25),
        ("¾", 0.75),
        ("1 ½", 1.5),
        ("1½", 1.5),
        ("1/2", 0.5),
        ("1,5", 1.5),
        ("1.5", 1.5),
        ("2", 2.0),
        ("dos", 2.0),
        ("media", 0.5),
        ("1 CAPSULA", 1.0),
        ("3 Capsulas", 3.0),
        ("2 tabletas", 2.0),
        ("1 parche", 1.0),
        ("8.5-17 g", None),
        ("dos gotas", None),
        ("5 ml", None),
        ("0", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_quantity(text, expected):
    assert ledd.parse_quantity(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("250 mg", [250.0]),
        ("0.25 mg", [0.25]),
        ("0,25 mg", [0.25]),
        ("2.5 MG", [2.5]),
        ("20mg", [20.0]),
        ("100 mcg", [0.1]),
        ("1 g", [1000.0]),
        ("25/250 mg", [25.0, 250.0]),
        ("100/25/200 mg", [100.0, 25.0, 200.0]),
        ("4 mg/24 h", [4.0]),
        ("250", [250.0]),
        ("2%", None),
        ("5 mg/ml", None),
        (None, None),
    ],
)
def test_parse_strength(text, expected):
    assert ledd.parse_strength(text) == expected


@pytest.mark.parametrize(
    ("name", "strength", "dose_note", "expected"),
    [
        ("Prolopa", "250 mg", None, "levodopa"),
        ("PROLOPA HBS", "125 mg", None, "levodopa_cr"),
        ("Sinemet CR", "50/200 mg", None, "levodopa_cr"),
        ("Levodopa/Carbidopa", "25/250 mg", None, "levodopa"),
        ("Stalevo", "100/25/200 mg", None, "levodopa_entacapona"),
        ("Parmital", "0.25 mg", None, "pramipexol"),
        ("Mirapex", "1 mg", None, "pramipexol"),
        ("Neupro", "4 mg", "(parche)", "rotigotina"),
        ("Rotigotina", "4 mg", None, "rotigotina"),
        ("Azilect®", "1 mg", None, "rasagilina"),
        ("Selegilina", "5 mg", None, "selegilina"),
        ("Selegilina", "1.25 mg", "sublingual", "selegilina_sublingual"),
        ("Xadago", "50 mg", None, "safinamida"),
        ("Ongentys", "50 mg", None, "opicapona"),
        ("Comtan", "200 mg", None, "entacapona"),
        ("Amantadina", "100 mg", None, "amantadina"),
        ("Losartán", "50 mg", None, None),
        ("Haloperidol", "1 mg", None, None),
        ("Sertralina", "50 mg", None, None),
    ],
)
def test_match_substance(name, strength, dose_note, expected):
    found = ledd.match_substance({"name": name, "strength": strength, "dose_note": dose_note})
    assert (found.key if found else None) == expected


def test_name_key_drops_numbers_and_accents():
    assert ledd.name_key("Losartán 50 mg") == "losartan"
    assert ledd.name_key("  Ácido   Fólico ") == "acido folico"


# ── Computation ──────────────────────────────────────────────────────────────


def test_family_style_list_matches_hand_calculation():
    """The family's hand method on a synthetic list: 3 × Prolopa 125 (100 mg
    levodopa each) + a 2 mg rotigotine patch × 30 + ½ of pramipexole 0.5 mg
    × 100 = 300 + 60 + 25 = 385 mg/day."""
    items = [
        item("Prolopa", "125 mg", "1", slot, id_=i)
        for i, slot in enumerate(("desayuno", "almuerzo", "tarde", "noche"), start=1)
        if slot != "tarde"
    ]
    items += [
        item("Rotigotina", "2 mg", "1", "desayuno", id_=5, dose_note="(parche)"),
        item("Parmital", "0.5 mg", "½", "noche", id_=6, dose_note="(0.25 mg total)"),
        item("Losartán", "50 mg", "1", "desayuno", id_=7),
        item("Vitamina D", "1000 UI", "1", "almuerzo", id_=8),
    ]
    result = ledd.compute(items)
    assert result["total_mg"] == 385.0
    rows = {c["substance"]: c for c in result["contributions"]}
    assert set(rows) == {"levodopa", "rotigotina", "pramipexol"}
    assert rows["levodopa"] == {
        "substance": "levodopa",
        "label": "Levodopa",
        "kind": "dose",
        "factor": 1.0,
        "source": ledd.TOMLINSON,
        "note": rows["levodopa"]["note"],
        "item_ids": [1, 2, 4],
        "names": ["Prolopa"],
        "daily_dose_mg": 300.0,
        "mg": 300.0,
    }
    assert (rows["rotigotina"]["daily_dose_mg"], rows["rotigotina"]["mg"]) == (2.0, 60.0)
    assert (rows["pramipexol"]["daily_dose_mg"], rows["pramipexol"]["mg"]) == (0.25, 25.0)
    assert all(c["source"] for c in result["contributions"])
    assert result["as_needed"] == result["not_calculated"] == result["pending"] == []


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        # Halves and quarters.
        ([item("Levodopa", "100 mg", "½")], 50.0),
        ([item("Levodopa", "100 mg", "¼")], 25.0),
        ([item("Levodopa", "100 mg", "1 ½")], 150.0),
        ([item("Ropinirol", "2 mg", "2 tabletas")], 80.0),
        # Weekday schedules: 3 of 7 days.
        ([item("Levodopa", "100 mg", "1", days=[1, 3, 5])], 42.86),
        ([item("Rasagilina", "1 mg", "1", days=[1, 2, 3, 4, 5, 6, 7])], 100.0),
        # CR factor.
        ([item("Prolopa HBS", "125 mg", "1")], 75.0),
        ([item("Sinemet CR", "50/200 mg", "1")], 150.0),
        # Combination strengths: the levodopa content.
        ([item("Prolopa", "125 mg", "2")], 200.0),
        ([item("Madopar", "200/50 mg", "1")], 200.0),
        ([item("Sinemet", "25/250 mg", "1")], 250.0),
        ([item("Stalevo", "100/25/200 mg", "1")], 133.0),
        # Pramipexole written as base (EU strength) → salt equivalent.
        ([item("Mirapexin", "0.18 mg", "1")], 25.71),
        # Other agonists / MAO-B / amantadine.
        ([item("Neupro", "8 mg", "1")], 240.0),
        ([item("Apomorfina", "3 mg", "1")], 30.0),
        ([item("Selegilina", "5 mg", "2")], 100.0),
        ([item("Amantadina", "100 mg", "1"), item("Amantadina", "100 mg", "1", "cena")], 200.0),
        # Safinamide: a fixed 150 whatever the dose (Jost 2023).
        ([item("Safinamida", "50 mg", "1")], 150.0),
        ([item("Safinamida", "100 mg", "1")], 150.0),
        # Nothing dopaminergic.
        ([item("Losartán", "50 mg", "1"), item("Haloperidol", "1 mg", "1")], None),
        ([], None),
    ],
)
def test_compute_table(items, expected):
    assert total(items) == expected


def test_comt_multiplies_the_levodopa_led():
    items = [
        item("Levodopa", "100 mg", "1", "desayuno", id_=1),
        item("Levodopa", "100 mg", "1", "cena", id_=2),
        item("Prolopa HBS", "125 mg", "1", "noche", id_=3),  # 75 LED
        item("Entacapona", "200 mg", "1", "desayuno", id_=4),
        item("Entacapona", "200 mg", "1", "cena", id_=5),
    ]
    result = ledd.compute(items)
    comt = next(c for c in result["contributions"] if c["substance"] == "entacapona")
    assert comt["base_mg"] == 275.0
    assert comt["mg"] == round(275 * 0.33, 2)
    assert comt["daily_dose_mg"] == 400.0
    assert comt["item_ids"] == [4, 5]
    assert result["total_mg"] == round(275 + 275 * 0.33, 2)


@pytest.mark.parametrize(
    ("name", "ratio"), [("Entacapona", 0.33), ("Tolcapona", 0.5), ("Opicapona", 0.5)]
)
def test_comt_ratios(name, ratio):
    items = [item("Levodopa", "100 mg", "2", id_=1), item(name, "50 mg", "1", "noche", id_=2)]
    assert total(items) == round(200 + 200 * ratio, 2)


def test_comt_without_levodopa_adds_zero_but_is_listed():
    result = ledd.compute([item("Entacapona", "200 mg", "1")])
    assert result["total_mg"] == 0.0
    assert result["contributions"][0]["mg"] == 0.0


def test_stalevo_levodopa_is_not_the_base_of_another_comt():
    items = [item("Stalevo", "100/25/200 mg", "1", id_=1), item("Opicapona", "50 mg", id_=2)]
    rows = {c["substance"]: c for c in ledd.compute(items)["contributions"]}
    assert rows["levodopa_entacapona"]["mg"] == 133.0
    assert rows["opicapona"]["base_mg"] == 0.0


@pytest.mark.parametrize(
    "extra",
    [
        {"slot": "segun_necesidad"},
        {"schedule_note": "Solo si tiene temblor"},
        {"instructions": "Cuando despierte en la madrugada"},
        {"schedule_note": "En caso de rigidez"},
    ],
)
def test_as_needed_and_conditional_doses_are_listed_apart(extra):
    items = [
        item("Levodopa", "100 mg", "2", id_=1),
        item("Prolopa", "250 mg", "½", "madrugada", id_=2, **extra)
        if "slot" not in extra
        else item("Prolopa", "250 mg", "½", id_=2, **extra),
    ]
    result = ledd.compute(items)
    assert result["total_mg"] == 200.0
    assert result["as_needed"] == [
        {
            "item_id": 2,
            "name": "Prolopa",
            "strength": "250 mg",
            "substance": "levodopa",
            "label": "Levodopa",
            "factor": 1.0,
            "source": ledd.TOMLINSON,
            "max_mg": 100.0,
        }
    ]


def test_only_as_needed_has_no_counted_total():
    result = ledd.compute([item("Levodopa", "100 mg", "1", "segun_necesidad")])
    assert result["has_dopaminergic"] is True
    assert result["total_mg"] is None
    assert result["as_needed"][0]["max_mg"] == 100.0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"strength": None}, "no_strength"),
        ({"strength": "2%"}, "bad_strength"),
        ({"name": "Prolopa", "strength": "300 mg"}, "ambiguous_strength"),
        ({"name": "Sinemet", "strength": "275 mg"}, "ambiguous_strength"),
        ({"name": "Ropinirol", "strength": "2/4 mg"}, "ambiguous_strength"),
        ({"quantity": None}, "no_quantity"),
        ({"quantity": "8.5-17 g"}, "bad_quantity"),
        ({"quantity": "dos gotas"}, "bad_quantity"),
        ({"schedule_note": "1 tableta cada 6 horas"}, "text_schedule"),
    ],
)
def test_unreadable_dose_is_not_calculated_never_guessed(kwargs, reason):
    base = {"name": "Levodopa", "strength": "100 mg", "quantity": "1", **kwargs}
    unreadable = item(base.pop("name"), base.pop("strength"), base.pop("quantity"), id_=9, **base)
    result = ledd.compute([item("Rasagilina", "1 mg", id_=1), unreadable])
    assert result["total_mg"] == 100.0  # the rasagiline only
    [row] = result["not_calculated"]
    assert row["item_id"] == 9
    assert row["reason"] == reason
    assert row["reason_text"] == ledd.REASONS[reason]


def learned(**kw):
    defaults = {
        "id": 1,
        "status": "proposed",
        "is_dopaminergic": True,
        "factor": 1.0,
        "mechanism": "other_dopaminergic",
        "confidence": "high",
    }
    return ledd.Learned(**{**defaults, **kw})


def test_unknown_name_without_row_adds_zero_silently():
    result = ledd.compute([item("Medicina Nueva", "10 mg")])
    assert result["has_dopaminergic"] is False
    assert result["total_mg"] is None


def test_proposed_dopaminergic_is_pending_and_not_counted():
    rows = {"medicina nueva mg": learned(factor=5.0)}
    items = [item("Levodopa", "100 mg", id_=1), item("Medicina Nueva", "10 mg", id_=2)]
    result = ledd.compute(items, {ledd.name_key("Medicina Nueva"): rows["medicina nueva mg"]})
    assert result["total_mg"] == 100.0
    assert result["pending"] == [
        {
            "item_id": 2,
            "name": "Medicina Nueva",
            "strength": "10 mg",
            "substance_id": 1,
            "proposed_factor": 5.0,
            "mechanism": "other_dopaminergic",
            "confidence": "high",
        }
    ]


@pytest.mark.parametrize(
    ("row", "pending"),
    [
        (learned(is_dopaminergic=False, factor=0.0, confidence="high"), False),
        (learned(is_dopaminergic=False, factor=0.0, confidence="low"), True),
        (learned(status="queued", is_dopaminergic=None, factor=None, confidence=None), False),
        (learned(status="failed", is_dopaminergic=None, factor=None, confidence=None), False),
        (learned(status="rejected", is_dopaminergic=False), False),
    ],
)
def test_which_proposals_wait_for_an_admin(row, pending):
    result = ledd.compute([item("Medicina Nueva")], {ledd.name_key("Medicina Nueva"): row})
    assert bool(result["pending"]) is pending
    assert result["contributions"] == []


def test_confirmed_substance_counts_with_its_factor():
    row = learned(status="confirmed", factor=5.0, label="Medicina nueva", source="Jost 2023")
    result = ledd.compute([item("Medicina Nueva", "10 mg", "½")], {"medicina nueva": row})
    assert result["total_mg"] == 25.0
    [c] = result["contributions"]
    assert (c["daily_dose_mg"], c["factor"], c["label"]) == (5.0, 5.0, "Medicina nueva")
    assert "Jost 2023" in c["source"]


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (920.0, 1040.0, {"before_mg": 920.0, "after_mg": 1040.0, "pct": 13}),
        (1000.0, 800.0, {"before_mg": 1000.0, "after_mg": 800.0, "pct": -20}),
        (None, 412.5, {"before_mg": None, "after_mg": 412.5, "pct": None}),
        (100.0, None, {"before_mg": 100.0, "after_mg": None, "pct": -100}),
        (412.5, 412.5, None),
        (None, None, None),
    ],
)
def test_change(before, after, expected):
    assert ledd.change({"total_mg": before}, {"total_mg": after}) == expected


def test_format_mg():
    assert ledd.format_mg(412.5) == "412.5"
    assert ledd.format_mg(800.0) == "800"
    assert ledd.format_mg(42.857) == "42.9"


def test_every_catalogue_entry_cites_a_source():
    keys = [e.key for e in ledd.CATALOGUE]
    assert len(keys) == len(set(keys))
    assert all(e.source and e.factor > 0 for e in ledd.CATALOGUE)
    assert ledd.TABLE_VERSION
