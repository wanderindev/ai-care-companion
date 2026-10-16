# AI Care Companion (n16chat)

An AI-powered medical record for families caring for elderly parents with chronic conditions. The app reads the family WhatsApp group (messages, voice notes, prescription photos, lab PDFs) and the Word documents the family already keeps, and turns them into structured records: a visit log (*bitácora*), a calendar, versioned medication lists and lab results with trends. The AI proposes every change and a family member approves it in a review queue («Pendientes»). A grounded chat answers questions and produces summaries and PDFs to hand to doctors.

The write-up explains the problem, the design decisions and the build: **[feliu.dev/projects/ai-care-companion](https://feliu.dev/projects/ai-care-companion)**.

> This repository is a **snapshot** of a private codebase, published for reference. It is refreshed occasionally and does not accept pull requests. Deployment-specific values (domain, hosts) are replaced with placeholders such as `app.example.com`.

## No real data

No real patient data is ever committed: tests and fixtures use synthetic data, and the real source files live only outside the repository. `docs/demo.md` describes a seed script that fills a local stack with invented patients, doctors, records and a synthetic WhatsApp export, which is how the write-up's screenshots were made.

## Stack

- **Backend:** FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, PostgreSQL 17, a Postgres-backed job queue (`FOR UPDATE SKIP LOCKED`) run by a worker process
- **Frontend:** React 19, Vite, Tailwind v4, TanStack Query; Spanish UI, mobile-first installable PWA
- **AI:** Anthropic (extraction, classification, daily analysis, chat with tool use); OpenAI for voice-note transcription only
- **Extension:** a Chrome MV3 extension that syncs the family group from WhatsApp Web (`extension/`)
- **Storage and email:** S3-compatible object storage (MinIO locally), SMTP for magic links and calendar invites

## Documentation

- [`docs/DESIGN.md`](docs/DESIGN.md): goals, data model, every feature, privacy rules
- [`CLAUDE.md`](CLAUDE.md): the contributor and agent guide, with the reasoning behind each defensive rule
- [`docs/demo.md`](docs/demo.md): the synthetic demo dataset
- [`docs/extension-api.md`](docs/extension-api.md), [`docs/import.md`](docs/import.md), [`docs/deploy.md`](docs/deploy.md)

## Local development

```bash
docker compose up --build
```

- App: http://localhost:5180
- API health: http://localhost:8010/api/v1/health
- Sign in: `docker compose exec backend python -m scripts.users add you@example.com --admin`, request a link at http://localhost:5180/entrar and open the link printed in `docker compose logs backend` (local environments log magic links instead of emailing them).
- Demo data: see [`docs/demo.md`](docs/demo.md).
- Tests and lint: `scripts/run-tests.sh` (see `CLAUDE.md`).
