# Demo data (local only)

Synthetic data for screenshots and live demos of the app, so no real patient
data is ever on screen (issue #142). Every person, doctor, clinic, lab, ID
number, phone and value is invented: phones are `+507 6000-00xx`, e-mails
`@example.com`, and the patients keep their display names Papá / Mamá.

| What | Where |
|---|---|
| Seed script | `backend/scripts/demo_seed.py` (records in `demo_seed_data.py`) |
| WhatsApp export | `backend/scripts/demo_data/WhatsApp Chat with Familia Benitez.zip` |
| Build / import the export | `backend/scripts/demo_export.py` |
| AI keys for the stack | `docker-compose.demo.yml` (opt-in overlay) |

Both scripts refuse to run unless `ENVIRONMENT=development`, the database is
the compose service `db:5432/n16chat` and file storage is the compose
`minio` service. There is no flag that skips the check.

## Seed and reset

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
docker compose exec backend python -m scripts.demo_seed
```

Re-running the seed **resets** the demo: it empties the data tables
(bitácora, appointments, doctors, medications, labs, WhatsApp imports,
suggestions, chat, jobs, audit log) and seeds again. Users and the two patient
rows stay. If the database holds records the seed did not create, it stops;
`--wipe` empties it anyway (or start clean with `docker compose down -v`).

What it creates: 3 family users (`demo@example.com` = Andrés, admin with
import access; Lucía; Marta), the patients' invented identities, 8 doctors,
60 bitácora entries over six months (6 left «Por revisar»), past appointments
marked done plus upcoming ones, four medication-list versions per patient
(Papá's levodopa / entacapone regimen gives an LEDD; EVITAR items for both),
bitácora medication events (some pending review) and 8 lab reports with
trends (Mamá: D-dimer, hemoglobin, TSH, INR; Papá: creatinine, vitamin D).
Mamá's last 45 days are the storyline: leg swelling → D-dimer + venous
Doppler → deep vein thrombosis → rivaroxabán (loading dose, then 20 mg) →
follow-up labs → cataract surgery under sedation that needs a cardiology
pre-operative assessment (upcoming appointments).

Dates are written for a story that ends on 2026-10-15 and are moved forward
by whole weeks to the current week, so the upcoming appointments stay ahead.

## WhatsApp import (fills «Pendientes»)

```bash
docker compose exec backend python -m scripts.demo_export import
docker compose exec backend python -m scripts.demo_export status   # repeat until analyze_import is done
```

This goes through the real `daily` pipeline in the worker: the voice note is
transcribed (OpenAI), the PDF is classified and read into a lab report
(Claude), and about two minutes after the import the daily analysis proposes
entries, appointments and medication changes, including the answer to the
«Bot: …» message. It needs `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in the
root `.env` and the stack started with `docker-compose.demo.yml`; one run
costs about USD 0.20. To import again, re-run the seed first (it clears the
import and its suggestions).

`demo_export build` rebuilds the zip (chat text and the lab PDF; `--voice`
re-synthesizes the voice note with OpenAI TTS, which needs `-e
OPENAI_API_KEY`). Commit the new zip after checking it holds nothing real.

## Signing in

```bash
# request a link at http://localhost:5180/entrar for demo@example.com, then:
docker compose logs backend | grep "DEV magic link"
```

## Before publishing anything

Check the demo files against the private denylist kept in `private/` (never
committed), e.g. `grep -i -F -f private/phi-denylist.txt -r backend/scripts`
plus the unzipped export, and expect no output.
