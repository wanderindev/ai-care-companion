"""The synthetic WhatsApp export for the demo (issue #142): build it, import it.

    # Rebuild the committed zip (lab PDF + chat text; the voice note is kept
    # from the existing zip unless --voice, which calls OpenAI TTS):
    docker compose exec -e OPENAI_API_KEY backend python -m scripts.demo_export build [--voice]

    # Import it through the real `daily` pipeline (worker: transcription,
    # media classification, lab extraction, daily analysis → «Pendientes»):
    docker compose exec backend python -m scripts.demo_export import
    docker compose exec backend python -m scripts.demo_export status

The zip is an Android-style export (``d/m/yy, H:MM - Sender: text``, media as
``<file> (archivo adjunto)``) with invented family members, a voice note
(``PTT-….opus``, synthetic speech) and a lab-result PDF from an invented lab.
Every name and value is fictional (``demo_seed_data``).

``import`` uses the same week shift as ``demo_seed``: when the story has to
move forward, the chat's timestamps, the dates written in its messages and
the PDF are rebuilt for the shifted calendar (the voice note is reused; it
names a weekday and date that stay a real future Tuesday). Same local-only
guard as the seed (:func:`scripts.demo_seed.check_local`), no override.
The worker needs ``ANTHROPIC_API_KEY`` and ``OPENAI_API_KEY``: start the stack
with ``docker-compose.demo.yml`` (``docs/demo.md``).
"""

import argparse
import html
import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import get_settings
from scripts import demo_seed_data as data
from scripts.demo_seed import NotLocal, check_local, shift_days

DATA_DIR = Path(__file__).resolve().parent / "demo_data"
ZIP_NAME = "WhatsApp Chat with Familia Benitez.zip"
CHAT_NAME = "WhatsApp Chat with Familia Benitez.txt"
VOICE_NAME = "PTT-20261014-WA0009.opus"
PDF_NAME = "Resultados_Arcoiris.pdf"

# What the voice note says (Andrés, after Papá's neurology visit).
VOICE_TEXT = (
    "Hola, les cuento cómo nos fue con Papá en el neurólogo. El doctor Salcedo lo "
    "encontró estable, pero la lentitud de las tardes sigue, sobre todo antes de la "
    "dosis de las cinco. Le va a agregar rasagilina de un miligramo, una tableta con el "
    "desayuno, todos los días. La levodopa y la entacapona siguen igual. Que sigamos con "
    "la fisioterapia dos veces por semana, y si aumentan los movimientos de la boca, que "
    "le avisemos. Le dio cita de control para el martes veinticuatro de noviembre a las "
    "nueve de la mañana."
)
VOICE_INSTRUCTIONS = (
    "Habla en español latinoamericano (Panamá), como un hijo que manda una nota de voz "
    "por WhatsApp a sus hermanos: tono natural, cercano, ritmo conversacional."
)
TTS_URL = "https://api.openai.com/v1/audio/speech"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "ash"

# (storyline date, "H:MM", sender, text). {VOICE} / {PDF} become attachments.
CHAT = [
    (
        "2026-10-12",
        "8:02",
        "Rosa",
        "Buenos días. Su papá amaneció bien, desayunó completo y "
        "ya se tomó las pastillas de las 7:30",
    ),
    ("2026-10-12", "8:05", "Lucía", "Gracias Rosa 🙏"),
    ("2026-10-12", "8:40", "Marta", "Mamá se tomó la presión: 146/88. Sigue un poco alta"),
    ("2026-10-12", "8:41", "Andrés", "Anotémoslo. Si sigue así se lo decimos a la Dra. Carvajal"),
    (
        "2026-10-12",
        "19:15",
        "Lucía",
        "¿Quién lleva a Papá el miércoles al neurólogo? Yo no puedo, tengo cierre en la oficina",
    ),
    ("2026-10-12", "19:20", "Andrés", "Yo lo llevo. ¿A qué hora es?"),
    ("2026-10-12", "19:21", "Lucía", "A las 10:00 con el Dr. Salcedo"),
    ("2026-10-12", "19:22", "Andrés", "Listo 👍"),
    (
        "2026-10-13",
        "7:55",
        "Rosa",
        "Buenos días. Su mamá dice que la pierna ya casi no le duele. Ya se puso la media",
    ),
    (
        "2026-10-13",
        "10:12",
        "Marta",
        "Ya agendé el Doppler de control de la pierna de Mamá: "
        "jueves 3 de diciembre a las 8:00 en Imagen Diagnóstica Costa Verde. Lo pidió el Dr. "
        "Larrinaga en la última consulta",
    ),
    ("2026-10-13", "10:15", "Lucía", "Perfecto, gracias Marta"),
    ("2026-10-13", "20:03", "Andrés", "¿Papá tiene que ir en ayunas mañana?"),
    ("2026-10-13", "20:05", "Lucía", "No, es solo consulta"),
    ("2026-10-14", "11:48", "Andrés", "{VOICE}"),
    ("2026-10-14", "11:50", "Lucía", "¡Gracias por el resumen! Entonces compro la rasagilina hoy"),
    ("2026-10-14", "11:52", "Andrés", "Sí, ya tengo la receta. La empieza mañana con el desayuno"),
    (
        "2026-10-14",
        "16:20",
        "Rosa",
        "Su papá hizo los ejercicios de la fisioterapia y caminó por el pasillo 15 minutos",
    ),
    (
        "2026-10-15",
        "7:10",
        "Marta",
        "Mamá fue temprano a sacarse sangre para los laboratorios de control del Dr. Larrinaga",
    ),
    ("2026-10-15", "13:05", "Marta", "{PDF}"),
    ("2026-10-15", "13:06", "Marta", "Ya salieron. El dímero D sigue bajando 🙌"),
    (
        "2026-10-15",
        "13:30",
        "Marta",
        "Esto me escribió la Dra. Carvajal cuando le mandé las "
        'presiones de la semana: "Buenas tardes. Con esas cifras vamos a subir el losartán de '
        "50 a 100 mg, una tableta en la mañana, a partir de mañana. Sigan tomando la presión y "
        'me envían los valores en una semana."',
    ),
    ("2026-10-15", "13:32", "Lucía", "Ok, yo le compro el de 100 mañana"),
    (
        "2026-10-15",
        "18:45",
        "Rosa",
        "Su mamá tuvo un poquito de sangrado en las encías al cepillarse, ya paró",
    ),
    (
        "2026-10-15",
        "18:50",
        "Lucía",
        "Bot: anota en la bitácora de Mamá que hoy tuvo un "
        "sangrado leve de encías al cepillarse, que paró solo en un par de minutos, sin otros "
        "sangrados",
    ),
    ("2026-10-15", "18:52", "Andrés", "Bien. Si se repite le avisamos al Dr. Larrinaga"),
    ("2026-10-15", "21:30", "Lucía", "Buenas noches familia ❤️"),
]

# The lab PDF (Mamá's follow-up labs, collected the morning of the last day).
PDF_COLLECTED = "2026-10-15"
PDF_RESULTS = [
    # (section, test, value, unit, reference, flag)
    ("COAGULACIÓN", "Tiempo de protrombina", "13.4", "s", "10.0 - 13.5", ""),
    ("COAGULACIÓN", "INR", "1.2", "", "0.8 - 1.2", ""),
    ("COAGULACIÓN", "Dímero D", "0.62", "µg/mL FEU", "< 0.50", "ALTO"),
    ("HEMATOLOGÍA", "Hemoglobina", "11.9", "g/dL", "12.0 - 16.0", "BAJO"),
    ("HEMATOLOGÍA", "Hematocrito", "35.8", "%", "36.0 - 46.0", "BAJO"),
    ("HEMATOLOGÍA", "Glóbulos blancos", "6.4", "10³/µL", "4.5 - 11.0", ""),
    ("HEMATOLOGÍA", "Plaquetas", "226", "10³/µL", "150 - 450", ""),
    ("QUÍMICA", "Creatinina", "1.0", "mg/dL", "0.6 - 1.1", ""),
]

MONTHS = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre"
).split()
_WRITTEN_DATE = re.compile(r"\b(\d{1,2}) de (" + "|".join(MONTHS) + r")\b")


def shift_written_dates(value: str, delta: timedelta, year: int) -> str:
    """Move «3 de diciembre» by ``delta`` (dates in messages follow the shift)."""

    def repl(match: re.Match) -> str:
        day = date(year, MONTHS.index(match.group(2)) + 1, int(match.group(1))) + delta
        return f"{day.day} de {MONTHS[day.month - 1]}"

    return _WRITTEN_DATE.sub(repl, value) if delta else value


def chat_text(delta: timedelta) -> str:
    lines = []
    for day, hhmm, sender, message in CHAT:
        when = data.d(day) + delta
        stamp = f"{when.day}/{when.month}/{when.year % 100:02d}, {hhmm}"
        if message == "{VOICE}":
            message = f"{VOICE_NAME} (archivo adjunto)"
        elif message == "{PDF}":
            message = f"{PDF_NAME} (archivo adjunto)"
        else:
            message = shift_written_dates(message, delta, data.ANCHOR.year)
        lines.append(f"{stamp} - {sender}: {message}")
    first = data.d(CHAT[0][0]) + delta
    header = (
        f"{first.day}/{first.month}/{first.year % 100:02d}, 7:00 - Los mensajes y las llamadas "
        "están cifrados de extremo a extremo. Nadie fuera de este chat, ni siquiera WhatsApp, "
        "puede leerlos ni escucharlos."
    )
    return "\n".join([header, *lines]) + "\n"


def lab_pdf_html(delta: timedelta) -> str:
    patient = data.PATIENTS["mama"]
    collected = data.d(PDF_COLLECTED) + delta
    age = (
        collected.year
        - patient["birth_date"].year
        - (
            (collected.month, collected.day)
            < (patient["birth_date"].month, patient["birth_date"].day)
        )
    )
    rows, section = [], None
    for sec, test, value, unit, ref, flag in PDF_RESULTS:
        if sec != section:
            rows.append(f'<tr class="sec"><td colspan="5">{html.escape(sec)}</td></tr>')
            section = sec
        cls = ' class="flag"' if flag else ""
        rows.append(
            f"<tr><td>{html.escape(test)}</td><td{cls}>{html.escape(value)}</td>"
            f"<td>{html.escape(unit)}</td><td>{html.escape(ref)}</td>"
            f"<td{cls}>{html.escape(flag)}</td></tr>"
        )
    fmt = collected.strftime("%d/%m/%Y")
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><style>
@page {{ size: Letter; margin: 18mm 16mm; }}
body {{ font-family: "DejaVu Sans", sans-serif; font-size: 10pt; color: #222; }}
.head {{ border-bottom: 3px solid #7b3fa0; padding-bottom: 6px; margin-bottom: 10px; }}
.lab {{ font-size: 16pt; font-weight: bold; color: #7b3fa0; }}
.small {{ font-size: 8.5pt; color: #555; }}
table {{ width: 100%; border-collapse: collapse; }}
.info td {{ padding: 2px 4px; }}
.res th {{ text-align: left; border-bottom: 1px solid #999; padding: 4px; font-size: 9pt; }}
.res td {{ padding: 3px 4px; border-bottom: 1px solid #eee; }}
.sec td {{ font-weight: bold; background: #f1e9f6; padding-top: 6px; }}
.flag {{ font-weight: bold; color: #b00020; }}
.foot {{ margin-top: 18px; font-size: 8pt; color: #666; }}
</style></head><body>
<div class="head"><div class="lab">{html.escape(data.LAB_NAME)}</div>
<div class="small">Calle Ficticia 123, Ciudad de Panamá · Tel. +507 6000-0099 ·
resultados@example.com</div></div>
<table class="info">
<tr><td><b>Paciente:</b> {html.escape(patient["full_name"].upper())}</td>
<td><b>Cédula:</b> {html.escape(patient["id_number"])}</td></tr>
<tr><td><b>Edad:</b> {age} años · <b>Sexo:</b> F</td>
<td><b>Orden:</b> 2026-104733</td></tr>
<tr><td><b>Médico:</b> Dr. Esteban Larrinaga Pupo</td>
<td><b>Fecha de toma:</b> {fmt} 07:20 · <b>Reporte:</b> {fmt} 12:41</td></tr>
</table>
<h3>RESULTADOS</h3>
<table class="res"><tr><th>Prueba</th><th>Resultado</th><th>Unidad</th>
<th>Valor de referencia</th><th></th></tr>
{"".join(rows)}
</table>
<p class="foot">Validado por: Lic. Tecnóloga Médica (firma electrónica).<br>
Documento de demostración: laboratorio, paciente y valores ficticios.</p>
</body></html>"""


def render_lab_pdf(delta: timedelta) -> bytes:
    from app.services.pdf.render import render

    return render(lab_pdf_html(delta))


def synthesize_voice(api_key: str) -> bytes:
    body = json.dumps(
        {
            "model": TTS_MODEL,
            "voice": TTS_VOICE,
            "input": VOICE_TEXT,
            "instructions": VOICE_INSTRUCTIONS,
            "response_format": "opus",  # Ogg Opus, like a WhatsApp voice note
        }
    ).encode()
    request = urllib.request.Request(
        TTS_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def build_zip(chat: str, voice: bytes, pdf: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(CHAT_NAME, chat)
        archive.writestr(VOICE_NAME, voice)
        archive.writestr(PDF_NAME, pdf)
    return buffer.getvalue()


def committed_zip() -> Path:
    return DATA_DIR / ZIP_NAME


def read_member(name: str) -> bytes:
    with zipfile.ZipFile(committed_zip()) as archive:
        return archive.read(name)


# ── Commands ─────────────────────────────────────────────────────────────────


def cmd_build(args) -> int:
    if args.voice:
        key = os.environ.get("OPENAI_API_KEY") or get_settings().openai_api_key
        if not key:
            print("OPENAI_API_KEY is needed to synthesize the voice note.", file=sys.stderr)
            return 2
        voice = synthesize_voice(key)
    elif committed_zip().exists():
        voice = read_member(VOICE_NAME)
    else:
        print("No zip yet: pass --voice to synthesize the voice note.", file=sys.stderr)
        return 2
    DATA_DIR.mkdir(exist_ok=True)
    content = build_zip(chat_text(timedelta(0)), voice, render_lab_pdf(timedelta(0)))
    committed_zip().write_bytes(content)
    print(f"Wrote {committed_zip().name} ({len(content)} bytes)")
    return 0


def export_for(today: date) -> bytes:
    """The zip to import ``today``: the committed one, or rebuilt for the shift."""
    delta = timedelta(days=shift_days(today))
    if not delta:
        return committed_zip().read_bytes()
    return build_zip(chat_text(delta), read_member(VOICE_NAME), render_lab_pdf(delta))


def cmd_import(args) -> int:
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services.appointments import today_local
    from app.services.storage import get_storage
    from app.services.whatsapp_import import upload

    settings = get_settings()
    if not settings.anthropic_api_key or not settings.openai_api_key:
        print(
            "WARNING: ANTHROPIC_API_KEY / OPENAI_API_KEY are not set in this container; the "
            "worker needs them (start the stack with docker-compose.demo.yml).",
            file=sys.stderr,
        )
    content = export_for(today_local())
    storage = get_storage()
    db = SessionLocal()
    try:
        email = data.USERS["andres"][0]
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print("Run `python -m scripts.demo_seed` first.", file=sys.stderr)
            return 3
        imp = upload.start_upload(
            db,
            storage,
            settings,
            user_id=user.id,
            mode="daily",
            filename=ZIP_NAME,
            size=len(content),
            patient_focus_id=None,
            date_order="dmy",
        )
        db.commit()
        offset = 0
        for index in range(imp.chunk_count):
            size = upload.expected_chunk_size(imp, index)
            upload.put_chunk(storage, imp, index, content[offset : offset + size])
            offset += size
        upload.complete_upload(db, storage, imp, user_id=user.id)
        db.commit()
        print(
            f"Import {imp.id} queued ({len(content)} bytes). Follow it with: "
            "python -m scripts.demo_export status"
        )
    finally:
        db.close()
    return 0


def cmd_status(args) -> int:
    from app.core.database import SessionLocal
    from app.models.job import Job
    from app.models.suggestion import Suggestion
    from app.models.whatsapp import WhatsappImport

    db = SessionLocal()
    try:
        for imp in db.scalars(select(WhatsappImport).order_by(WhatsappImport.id)):
            analysis = (imp.stats or {}).get("analysis") or {}
            print(
                f"import {imp.id}: {imp.status}; messages={(imp.stats.get('messages') or {}).get('new')}; "
                f"analysis days_done={analysis.get('days_done')} "
                f"cost_usd={analysis.get('cost_usd')}"
            )
        jobs = db.execute(
            select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)
        ).all()
        for kind, status, count in sorted(jobs):
            print(f"job {kind}: {status} x{count}")
        pending = db.execute(
            select(Suggestion.kind, func.count())
            .where(Suggestion.status == "pending")
            .group_by(Suggestion.kind)
        ).all()
        print("pending suggestions: " + (", ".join(f"{k}={n}" for k, n in pending) or "none"))
    finally:
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="rebuild the committed zip")
    build.add_argument("--voice", action="store_true", help="re-synthesize the voice note")
    sub.add_parser("import", help="import the zip through the daily pipeline")
    sub.add_parser("status", help="imports, jobs and pending suggestions")
    args = parser.parse_args(argv)
    try:
        check_local(get_settings())
    except NotLocal as exc:
        print(f"REFUSED: demo data is for the local compose stack only ({exc}).", file=sys.stderr)
        return 2
    return {"build": cmd_build, "import": cmd_import, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
