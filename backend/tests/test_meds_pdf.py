"""Printable medication list (GET /patients/{id}/meds/print.pdf), synthetic data."""

import io
from datetime import date, timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.models.medication import SLOTS, MedListVersion
from app.services import pdf
from app.services.appointments import today_local


def url(patient, query=""):
    return f"/api/v1/patients/{patient.id}/meds/print.pdf{query}"


def med(name, slot="desayuno", **kw):
    return {"name": name, "slot": slot, "quantity": "1", **kw}


def pdf_text(content: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return " ".join(text.split()), len(reader.pages)


@pytest.fixture
def papa(patients):
    return patients[0]


def save(client, patient, expected, items, avoid=()):
    resp = client.put(
        f"/api/v1/patients/{patient.id}/meds",
        json={"expected_version": expected, "items": items, "avoid": list(avoid)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] is True
    return resp.json()


@pytest.fixture
def saved(auth_client, papa):
    """Version 1 of a synthetic list."""
    return save(
        auth_client,
        papa,
        0,
        [
            med("Medicina A", strength="250 mg", quantity="1/2", time="06:00", days=[1, 3, 5]),
            med("Medicina B", dose_note="(125 mg total)", instructions="con agua"),
            med("Medicina C", slot="cena", quantity="1 1/2", schedule_note="vie 2; lun 5"),
            med("Medicina R", slot="segun_necesidad", quantity="1", instructions="si hay dolor"),
            med("Suplemento S", slot="otros", quantity="2 cápsulas"),
        ],
        [{"substance": "Sustancia X", "reason": "reacción sintética"}],
    )


def test_requires_auth(db_client, patients):
    assert db_client.get(url(patients[0])).status_code == 401


def test_unknown_patient_and_version(auth_client, papa, saved):
    assert auth_client.get("/api/v1/patients/999/meds/print.pdf").status_code == 404
    assert auth_client.get(url(papa, "?version=9")).status_code == 404
    assert auth_client.get(url(papa, "?version=0")).status_code == 422


def test_current_list_pdf(auth_client, papa, saved, test_db):
    papa.full_name = "Paciente Sintético Uno"
    papa.id_number = "0-000-0000"
    test_db.commit()

    resp = auth_client.get(url(papa))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["cache-control"] == "no-store"
    today = today_local().isoformat()
    assert resp.headers["content-disposition"] == (
        f'inline; filename="medicamentos-papa-{today}.pdf"'
    )
    assert resp.content.startswith(b"%PDF-")

    text, pages = pdf_text(resp.content)
    assert pages == 1
    for expected in (
        "LISTA DE MEDICAMENTOS",
        "Paciente Sintético Uno",
        "CIP: 0-000-0000",
        f"Desde {pdf.long_date(today_local())}",
        "DESAYUNO",
        "CENA",
        "SEGÚN NECESIDAD (RESCATE)",
        "OTROS MEDICAMENTOS",
        "Medicina A 250 mg",
        "06:00 · lun, mié, vie",
        "(125 mg total)",
        "con agua",
        "vie 2; lun 5",
        "2 cápsulas",
        "EVITAR",
        "Sustancia X",
        "reacción sintética",
        f"Impreso el {pdf.long_date(today_local())} · Versión 1",
        "Página 1 de 1",
    ):
        assert expected in text, expected
    # Fractions are shown as glyphs (DejaVu Sans has them; no tofu).
    assert "½" in text and "1½" in text and "1/2" not in text
    # Slots in printed-sheet order.
    assert text.index("DESAYUNO") < text.index("CENA") < text.index("SEGÚN NECESIDAD")
    assert text.index("SEGÚN NECESIDAD") < text.index("OTROS MEDICAMENTOS")


def test_fallbacks_without_private_fields(auth_client, papa, saved):
    text, _ = pdf_text(auth_client.get(url(papa)).content)
    assert "Papá" in text
    assert "CIP" not in text


def test_empty_list(auth_client, papa):
    resp = auth_client.get(url(papa))
    assert resp.status_code == 200
    text, pages = pdf_text(resp.content)
    assert pages == 1
    assert "No hay medicamentos activos" in text and "Desde" not in text
    assert "Versión" not in text


def test_list_note_markdown_table_and_escaping(auth_client, papa, saved, test_db):
    version = test_db.scalar(select(MedListVersion).where(MedListVersion.patient_id == papa.id))
    version.list_note = (
        "Pauta de **Medicina A**:\n\n"
        "| Hora | Dosis |\n|---|---|\n| 06:00 | ½ |\n| 14:00 | ¼ |\n\n"
        "<script>alert(1)</script> <b>negrita</b> ![x](http://example.invalid/x.png)"
    )
    test_db.commit()

    from app.services.pdf import render_html
    from app.services.pdf.meds import build_context

    html = render_html(
        "meds.html",
        build_context(
            papa,
            {"items": [], "avoid": [], "list_note": version.list_note},
            version_no=1,
            since=None,
            printed_on=date(2026, 1, 2),
            today=date(2026, 1, 2),
        ),
    )
    assert "<table>" in html and "<th>Hora</th>" in html and "<td>14:00</td>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>negrita</b>" not in html and "&lt;b&gt;negrita&lt;/b&gt;" in html
    assert "<img" not in html

    text, _ = pdf_text(auth_client.get(url(papa)).content)
    assert "NOTAS DE LA LISTA" in text
    assert "Hora Dosis 06:00 ½ 14:00 ¼" in text
    assert "<script>alert(1)</script>" in text


def test_user_text_is_escaped(auth_client, papa):
    save(auth_client, papa, 0, [med("<i>Medicina</i> & Co", instructions="<b>x</b>")])
    from app.services.pdf.meds import build_context

    context = build_context(
        papa,
        {"items": [{"name": "<i>M</i>", "slot": "desayuno"}], "avoid": []},
        version_no=1,
        since=None,
        printed_on=date(2026, 1, 2),
        today=date(2026, 1, 2),
    )
    assert "<i>M</i>" not in pdf.render_html("meds.html", context)
    text, _ = pdf_text(auth_client.get(url(papa)).content)
    assert "<i>Medicina</i> & Co" in text and "<b>x</b>" in text


def test_version_param_prints_the_snapshot(auth_client, papa, saved):
    body = save(
        auth_client,
        papa,
        1,
        [
            {
                k: v
                for k, v in item.items()
                if k not in {"prescriber", "sort_order", "created_by", "updated_by"}
                and k not in {"created_at", "updated_at"}
            }
            for group in saved["slots"]
            for item in group["items"]
            if item["name"] != "Medicina B"
        ]
        + [med("Medicina Nueva", slot="noche")],
    )
    assert body["version_no"] == 2

    current, _ = pdf_text(auth_client.get(url(papa)).content)
    assert "Medicina Nueva" in current and "Medicina B" not in current
    assert "Versión 2" in current

    resp = auth_client.get(url(papa, "?version=1"))
    assert resp.status_code == 200
    old, _ = pdf_text(resp.content)
    assert "Medicina B" in old and "Medicina Nueva" not in old
    assert "Versión 1" in old and "Sustancia X" in old


def test_future_start_and_end_dates(auth_client, papa):
    today = today_local()
    later = (today + timedelta(days=10)).isoformat()
    until = (today + timedelta(days=20)).isoformat()
    save(auth_client, papa, 0, [med("Medicina F", start_date=later, end_date=until)])
    text, _ = pdf_text(auth_client.get(url(papa)).content)
    assert f"Empieza el {pdf.long_date(later)}" in text
    assert f"Hasta el {pdf.long_date(until)}" in text


def test_long_list_breaks_pages(auth_client, papa):
    items = [
        med(f"Medicina {i:02d}", slot=slot, schedule_note="pauta larga " * 12)
        for i, slot in enumerate(["desayuno", "almuerzo", "cena", "noche"] * 12)
    ]
    save(auth_client, papa, 0, items)
    text, pages = pdf_text(auth_client.get(url(papa)).content)
    assert 2 <= pages <= 8
    assert f"Página {pages} de {pages}" in text
    assert "Medicina 47" in text


def test_no_external_fetch():
    # The fetcher refuses every URL, so even an <img> reaching the HTML is never loaded.
    from app.services.pdf.render import render

    png = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
        "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    html = (
        f'<p>x</p><img src="{png}"><img src="file:///etc/hostname">'
        '<img src="http://127.0.0.1:9/x.png">'
    )
    content = render(html)
    assert content.startswith(b"%PDF-") and b"/Image" not in content


def test_render_failure_is_503(auth_client, papa, saved, monkeypatch):
    monkeypatch.setattr(pdf, "RENDERER", pdf.RENDERER.with_name("missing.py"))
    resp = auth_client.get(url(papa))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "No se pudo generar el PDF. Inténtalo de nuevo."


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1/2", "½"), ("1 1/2", "1½"), ("1/4 tableta", "¼ tableta"), ("3/4", "¾"), ("5/8", "5/8")],
)
def test_format_quantity(text, expected):
    assert pdf.format_quantity(text) == expected


def test_long_date():
    assert pdf.long_date(date(2026, 9, 30)) == "30 de septiembre de 2026"
    assert pdf.long_date("2026-01-05") == "5 de enero de 2026"
    assert pdf.long_date(None) == ""


# Synthetic lists the size of the real ones (counts and lengths only, measured 2026-10):
# Papá has 31 active items in 9 slots (20 with instructions of up to ~70 characters, 8
# with a dose or schedule note, the longest ~120 characters, 4 with a reason, none with
# a prescriber, names + strength up to ~78 characters, quantities up to 10 characters
# such as «2 cápsulas»), 7 EVITAR rows and a ~270-character regimen table; Mamá has 20
# items in 6 slots. The printed list goes on the wall where the pill boxes are filled,
# so it must stay at most two Letter pages (#97).
INSTRUCTIONS = (
    "Tomar con un vaso de agua completo",
    "Con comida; no masticar ni partir la tableta",
    "Separar 2 horas de los suplementos de calcio y del hierro",
    "En ayunas, 30 minutos antes del desayuno, sentado y con agua sola",
    "Si la presión está por debajo de 100, no darla y avisar al médico ya",
)
NOTES = ("(250 mg en total)", "lun, mié y vie 1; los demás días ½", "(2 gotas en cada ojo)")
LONG_NOTE = (
    "Semana 1: 1 tableta en la mañana; semana 2: 1 en la mañana y ½ en la noche; "
    "desde la semana 3: 1 en la mañana y 1 en la noche"
)
QUANTITIES = ("1", "1/2", "2", "1 1/2", "2 cápsulas", "1 sobre")


def synthetic_list(items, slots, with_instructions, with_notes, avoid):
    used = SLOTS[: slots - 1] + ("otros",)
    rows = []
    for i in range(items):
        row = {
            "name": f"Medicina Sintética {chr(65 + i % 26)}{i:02d}",
            "strength": f"{(i % 9 + 1) * 25} mg",
            "slot": used[i % slots],
            "quantity": QUANTITIES[i % len(QUANTITIES)],
            "time": "07:00" if i % 5 == 0 else None,
            "days": [1, 3, 5] if i % 7 == 0 else None,
            "instructions": INSTRUCTIONS[i % 5] if i < with_instructions else None,
            "reason": "control sintético de la presión y del colesterol" if i % 8 == 0 else None,
        }
        if i == 0:
            row["name"] = "Medicina Sintética Combinada de Liberación Prolongada A00"
            row["strength"] = "50 mg / 12,5 mg / 5 mg"
        if i < with_notes:
            row["dose_note" if i % 2 else "schedule_note"] = NOTES[i % 3]
        if i == 1:
            row["schedule_note"] = LONG_NOTE
        rows.append(row)
    note = (
        "Pauta de **Medicina Sintética A00** (dosis según el día):\n\n"
        "| Día | Mañana | Noche |\n|---|---|---|\n"
        "| Lunes a viernes | 1 tableta | ½ tableta |\n"
        "| Sábado | ½ tableta | ½ tableta |\n"
        "| Domingo | nada | ½ tableta |\n\n"
        "Revisar la presión antes de la dosis de la mañana."
    )
    return {
        "items": rows,
        "avoid": [
            {"substance": f"Sustancia Evitar {i}", "reason": "reacción alérgica sintética"}
            for i in range(avoid)
        ],
        "list_note": note,
    }


def render_snapshot(snapshot) -> bytes:
    from types import SimpleNamespace

    from app.services.pdf.meds import TEMPLATE, build_context

    patient = SimpleNamespace(
        full_name="Paciente Sintético Con Nombre Largo", display_name="Papá", id_number="0-000-0"
    )
    context = build_context(
        patient,
        snapshot,
        version_no=12,
        since=date(2026, 1, 2),
        printed_on=date(2026, 1, 2),
        today=date(2026, 1, 2),
    )
    return pdf.render_pdf(TEMPLATE, context)


@pytest.mark.parametrize(
    ("label", "size"),
    [
        ("papa", (31, 9, 20, 8, 7)),
        ("papa + 2", (33, 9, 22, 9, 8)),
        ("mama", (20, 6, 19, 4, 4)),
    ],
)
def test_realistic_list_fits_two_pages(label, size):
    content = render_snapshot(synthetic_list(*size))
    text, pages = pdf_text(content)
    assert pages <= 2, label
    # Nothing dropped to get there.
    assert "EVITAR" in text and f"Sustancia Evitar {size[4] - 1}" in text
    assert f"Medicina Sintética {chr(65 + (size[0] - 1) % 26)}{size[0] - 1:02d}" in text
    assert "Lunes a viernes" in text


def test_long_section_repeats_its_name_on_the_next_page():
    snapshot = synthetic_list(60, 1, 60, 0, 0)
    snapshot["list_note"] = None
    for item in snapshot["items"]:
        item["slot"] = "desayuno"
    reader = PdfReader(io.BytesIO(render_snapshot(snapshot)))
    assert len(reader.pages) >= 2
    for number, page in enumerate(reader.pages):
        text = " ".join((page.extract_text() or "").split())
        # The slot name is the table's header row: printed again on every page.
        assert text.count("DESAYUNO") == 1
        assert number == 0 or text.startswith("DESAYUNO")
        assert "Medicina Sintética" in text


def test_long_quantity_words_stay_in_their_column():
    """A quantity word must wrap inside the quantity column, never print over the
    details beside it (text extraction cannot see an overprint, so this checks the
    laid-out boxes)."""
    from types import SimpleNamespace

    from weasyprint import HTML
    from weasyprint.formatting_structure import boxes

    from app.services.pdf.meds import TEMPLATE, build_context

    quantities = ["2 cápsulas", "15 unidades", "1 cucharada", "2 inhalaciones", "1 " + "x" * 48]
    snapshot = {
        "items": [
            {"name": f"Medicina {i}", "slot": "desayuno", "quantity": q, "time": "07:00"}
            for i, q in enumerate(quantities)
        ],
        "avoid": [],
    }
    context = build_context(
        SimpleNamespace(full_name="", display_name="Papá", id_number=None),
        snapshot,
        version_no=1,
        since=None,
        printed_on=date(2026, 1, 2),
        today=date(2026, 1, 2),
    )
    document = HTML(string=pdf.render_html(TEMPLATE, context)).render()
    cells = [
        box
        for page in document.pages
        for box in page._page_box.descendants()
        if isinstance(box, boxes.TableCellBox) and "qty" in (box.element.get("class") or "")
    ]
    assert len(cells) == len(quantities)
    words = [
        box.text.strip()
        for cell in cells
        for box in cell.descendants()
        if isinstance(box, boxes.TextBox)
    ]
    # Words up to 8 letters (the longest on the real lists) are never cut.
    for word in ("cápsulas", "unidades"):
        assert any(word in text for text in words), words
    for cell in cells:
        right = cell.content_box_x() + cell.width
        texts = [box for box in cell.descendants() if isinstance(box, boxes.TextBox)]
        assert texts
        for text in texts:
            assert text.position_x + text.width <= right + 0.5, text.text
