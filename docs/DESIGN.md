# n16chat — Design Document

Family web app to keep elderly parents' medical information in one shared place:
the visit log (*bitácora*), appointments, medication lists, lab results, and a
chat to ask questions about all of it. Built for a handful of siblings, used
mostly from phones.

> **Privacy rule for this repo:** no real patient data is ever committed — no
> source `.docx` files, no WhatsApp exports, no names + ID numbers in fixtures.
> Tests use synthetic data. The real source files live only on the operator's
> machine (gitignored) and in production storage.

---

## 1. Goals and non-goals

**Goals**

1. Replace the manual Word documents (bitácora + two medication lists) with
   structured, shared, editable records.
2. Make adding information easy for non-technical family members (mobile-first,
   Spanish UI, magic-link login, photo uploads).
3. Bring the WhatsApp family group (where visit summaries, lab PDFs and Rx
   photos are shared) into the app with AI help, always with a human
   approving changes to the medical record.
4. Let anyone ask questions in a shared chat and get PDFs/Excel files to hand
   to doctors.

**Non-goals (v1)**: finances, caregiver (non-family) accounts, two-way
calendar sync, medical advice of any kind. The AI organizes and retrieves
what the family and doctors already said; it never recommends treatment.

## 2. Users and access

- 3–6 family members. No public sign-up: an **allowlist** of emails managed by
  an admin (the operator) via a CLI command and, later, a small admin screen.
- **Login by email magic link** (no passwords):
  - Link valid 15 minutes, single use, stored as a hash.
  - Requesting a link for an unknown email returns the same response as a
    known one (no enumeration). Rate-limited per email and per IP.
  - The link is `https://app.example.com/acceso#token=…`: the token sits in the
    URL fragment so it never reaches a server log, and only the SPA's
    `POST /auth/verify` consumes it (email scanners that prefetch the link
    with a GET just load the SPA).
  - Rate limits (5 requests per email and 20 per IP per hour) are counted
    from `magic_link_tokens` rows, so they hold across uvicorn workers;
    requests for unknown emails are recorded too (no user, token never sent).
    Over a limit the response is still the same 200; nothing is written or
    sent. The client IP comes from the edge's `X-Real-IP` (see
    `docs/deploy.md`), never from the client-controlled `X-Forwarded-For`.
- **Sessions**: opaque random token in an `HttpOnly`, `Secure`,
  `SameSite=Lax` cookie; hashed server-side. **Sliding 30-day expiry**
  (requirement: ≥ 14 days) — each authenticated request older than 1 hour
  since the last refresh extends it. Logout deletes the session.
- Roles: `admin` (manage users, run imports) and `member`. Everyone sees both
  patients; everyone can edit.
- Every write is recorded in an **audit log** (who, when, before/after).

## 3. UI principles

- **Spanish UI only** (code, identifiers and docs in English).
- Mobile-first PWA (installable, home-screen icon). Large touch targets,
  bottom navigation, bottom sheets instead of side drawers on phones.
- **Patient switcher** at the top: *Papá* / *Mamá* / *Ambos*. Remembered per
  device.
- Bottom nav: **Bitácora · Calendario · Medicamentos · Labs · Chat**. A
  **Pendientes** badge (AI suggestions waiting for review) lives in the header.
- Desktop gets wider layouts (real tables) but is secondary.

## 4. Architecture

Mirrors the operator's other projects (see `panama-in-context`):

```
React 19 + Vite + Tailwind v4 (PWA, react-router, TanStack Query/Table)
        │  HTTPS, cookie session
FastAPI (Python 3.12, SQLAlchemy 2.0, Alembic, Pydantic v2)
   ├── PostgreSQL 17 (DO managed) — all structured data, full-text search
   ├── DO Spaces (private bucket) — attachments, lab PDFs, exports, zips
   ├── Worker process (same image) — imports, OCR/extraction, transcription
   ├── Anthropic API — extraction, classification, chat (tool use)
   ├── OpenAI transcription API — voice notes only (Claude does not take audio input)
   └── Resend (SMTP) — magic links, calendar invites
```

- **Deploy**: GitHub Actions (like panama-in-context's `deploy.yml`) builds
  `ghcr.io/wanderindev/n16chat-backend` (also runs the worker) and
  `ghcr.io/wanderindev/n16chat-frontend` (GHCR, because the DO registry is at
  its 5-repo cap; GHCR container storage/bandwidth is currently free).
  Watchtower on the `pic` droplet pulls them. Domain **`app.example.com`** (+
  `www`), served through the shared edge (`pic-frontend` nginx) with its own
  Let's Encrypt certificate. Same origin for the SPA and `/api` (no CORS).
  See `docs/deploy.md` for the infrastructure inventory.
- **Background jobs**: a `jobs` table polled by the worker
  (`SELECT … FOR UPDATE SKIP LOCKED`). No Redis/Celery. Jobs report progress
  (`progress`, `message`) that the UI polls.
- **Search**: Postgres full-text search with a Spanish config + `unaccent`.
  The dataset is small (hundreds of entries); no vector DB needed.
- **Models**: model IDs come from settings, not code. Defaults:
  extraction `claude-opus-5-5` (`EXTRACTION_MODEL`), WhatsApp media
  classification `claude-haiku-5-5` (`CLASSIFICATION_MODEL`: a four-way
  decision plus one line, with vision/PDF input), chat `claude-sonnet-5-5`
  (`CHAT_MODEL`), chat titles `claude-haiku-5-5` (`CHAT_TITLE_MODEL`),
  voice-note transcription OpenAI `gpt-transcribe` (`TRANSCRIPTION_MODEL`).
  Prompt caching for the stable system prompt + tool definitions.
- **PDF output**: HTML (Jinja templates) → WeasyPrint. **Excel**: openpyxl.
- **Files**: stored in Spaces under `<kind>/<yyyy>/<sha256>.<ext>`, deduped by
  SHA-256, served only through short-lived presigned URLs (≤ 10 min) after an
  auth check.
- **Observability**: Sentry optional; request bodies, cookies and any medical
  text are scrubbed. Logs never contain medical content.

## 5. Data model

All tables have `id` (UUID or bigint), `created_at`, `updated_at`; user-edited
tables also `created_by`, `updated_by`.

### 5.1 People

| Table | Key fields |
|---|---|
| `users` | email (unique, lowercased), display_name, role (`admin`/`member`), is_active, email_invites (opt-in calendar invites, default false) |
| `magic_link_tokens` | user_id, token_hash, expires_at, used_at, requested_ip |
| `sessions` | user_id, token_hash, expires_at, last_seen_at, user_agent |
| `patients` | slug (`papa`, `mama`), display_name, full_name, id_number (CIP), birth_date, diagnoses (text), notes |
| `doctors` | name (canonical, e.g. "Dra. Ana Pérez"), aliases (text[] — "Anita", "Dra. Pérez"…), specialty, phone, email, clinic, address, notes, is_active. Matching free text to a doctor (importers) is accent/case/title-insensitive on name or any alias (`app.services.doctors`). Admins **merge** duplicates: every row pointing at the duplicate moves to the kept doctor, its name and aliases become aliases, empty contact fields are filled, and the duplicate is deleted (audit action `merge`) |
| `audit_log` | user_id (NULL for CLI changes), action, entity, entity_id, before (jsonb), after (jsonb), created_at (= when) |

### 5.2 Bitácora

`log_entries`

| Field | Notes |
|---|---|
| patient_id | |
| entry_date | date (time optional: `entry_time`) |
| doctor_id | nullable — family observations have no doctor |
| source_label | free text as written ("FisioCentro - correo", "Terapia respiratoria") when there is no doctor or to preserve the original |
| entry_type | `consulta_control` (scheduled follow-up), `consulta_enfermedad` (onset of illness), `visita_domiciliaria`, `teleconsulta`, `terapia`, `vacuna`, `procedimiento`, `hospitalizacion`, `examen`, `observacion_familiar`, `otro` |
| channel | `presencial`, `domicilio`, `whatsapp`, `telefono`, `correo`, null |
| title | short reason/diagnosis ("Balanitis", "Neumonía bilateral") |
| indications | markdown — what the doctor said/ordered |
| family_notes | markdown — family comments (kept separate from what the doctor said) |
| review_status | `ok`, `pending_review` (imported/AI-created, not yet confirmed) |
| origin | `import_docx`, `whatsapp`, `manual`, `chat` |
| origin_ref | jsonb (source doc + position, message ids…) |
| search_vector | generated tsvector (title + indications + family_notes + source_label), `spanish` config on unaccented text; `q` matches it plus the doctor's name/aliases, every word as a prefix, accent/case-insensitive |

`attachments` — generic: owner_type (`log_entry`, `lab_report`, `chat_message`,
`appointment`, `whatsapp_message`), owner_id, kind (`images`/`documents`/`audio`),
storage_key, filename, mime (sniffed from the bytes), size, sha256, uploaded_by.
Files are uploaded first (owner NULL) and linked to their owner when it is
saved; rows with the same sha256 share one stored object. Photos from the phone camera are downscaled
client-side (max 2560 px) before upload.

### 5.3 Appointments

`appointments`: patient_id, doctor_id (nullable), starts_at (timestamptz,
America/Panama), duration_min, all_day, location, reason, notes, status
(`programada`, `realizada`, `cancelada`), created_from_entry_id (the entry
where "Próxima cita…" was written), result_entry_id (the bitácora entry
created when it happened).

- **Time**: the API reads ISO 8601 with an offset (a naive value is Panamá
  time) and returns `starts_at` on the Panamá clock (`-05:00`; no DST). An
  **all-day** appointment has `all_day = true` and `starts_at` at 00:00
  Panamá of its day (`duration_min`, default 30, is then ignored).
  `GET /appointments` filters by patient (repeatable), doctor, status and
  `date_from`/`date_to` (inclusive Panamá days), ordered by `starts_at`.
- **Lifecycle**: new appointments are `programada`; `PATCH status=cancelada`
  cancels, `status=realizada` + `result_entry_id` (same patient) records the
  visit; `DELETE` is a hard delete for mistakes (the audit row keeps it).
  Both entry links are `ON DELETE SET NULL`: deleting an entry never
  deletes an appointment.
- **From the bitácora**: `next_appointment` on `POST/PATCH /log-entries`
  (`{date, time?, doctor_id?, location?, reason?, notes?}`; no doctor_id →
  the entry's doctor) creates or updates the entry's one appointment
  (`created_from_entry_id` is unique) and sets it back to `programada`;
  `null` **cancels** it (never deletes; filling it again restores it). A
  `realizada` one cannot be changed from the entry (400). A `programada`
  one follows its entry to another patient. `GET /log-entries/{id}`
  returns it as `next_appointment`.
- **Backfill** of imported entries: `python -m scripts.appointments
  backfill` (rules in `docs/import.md`): one appointment per visit (patient,
  day, time when given, doctor), so a "Próxima cita" repeated in later
  entries is not duplicated, and appointments deleted in the app are not
  recreated. The entry form does
  not dedupe: what the family types explicitly is created as typed.

`calendar_feeds`: user_id, token_hash, created_at, revoked_at — one secret
ICS URL per user.

### 5.4 Medications

The printed list is organized by **time slot**, so slot is first-class.

`med_items` (current state of a patient's list)

| Field | Notes |
|---|---|
| patient_id | |
| name | "Prolopa", "Vitamina D 5,000 UI" |
| strength | "250 mg" |
| quantity | text: "1", "½", "¼", "2", "3 cápsulas", "8.5–17 g" |
| dose_note | "(125 mg total)" |
| slot | `al_despertar`, `desayuno`, `manana`, `almuerzo`, `tarde`, `cena`, `noche`, `madrugada`, `segun_necesidad`, `otros` |
| time | optional exact time ("06:00") |
| days | `smallint[]` of ISO weekdays (1 = lunes … 7 = domingo), sorted and unique; null = daily (all seven days are stored as null) |
| schedule_note | free text for tapers/specific dates ("vie 25 sep; lun 28 sep…") |
| instructions | "30 min antes del desayuno", "con la cena" |
| prescriber_id, reason | optional |
| start_date, end_date | end_date set = stops showing after that date (still listed on end_date itself); a future start_date is still listed |
| sort_order | within slot |
| source_entry_id | bitácora entry that introduced/changed it |

`prescriber_id` → doctors is `ON DELETE SET NULL` (doctor merge repoints
it); every `source_entry_id` → log_entries is `ON DELETE SET NULL` (a
bitácora delete is hard).

`med_avoid` (EVITAR list): patient_id, substance, reason, source_entry_id.
A row removed from the list is deleted (snapshots and the audit log keep it).

`med_check_dismissals` (migration 0015): patient_id, entry_id (unique,
`ON DELETE CASCADE`), created_by/created_at — the bitácora entries the
family marked «Ya está reflejado» in the out-of-date check (§6.4).

`med_list_versions`: patient_id, version_no (unique per patient, from 1),
snapshot (jsonb of all items + avoid list), change_summary (text,
auto-generated + editable), changed_by, changed_at, list_note (markdown
text that belongs to the whole list, e.g. a regimen table such as one drug's
schedule by hour), effective_date (the sheet's "Desde <fecha>"; null = the
date of changed_at). Both are set in the editor's «Revisar cambios» step
(«Desde» defaults to today) and a save that leaves them out (an approved
`med_change`, an API call without them) carries them forward; `null` clears
them (#135). They are also snapshot keys and part of the diff and summary
(«Desde» pasó de … a …; «se modificó la nota de la lista»). **Every save that
changes something creates a version**; the diff view compares two snapshots
(added / removed / changed fields). Version 1 = import of the current Word
lists. Snapshot shape, diff shape and summary rules live in the pure module
`app/services/meds_diff.py`: items are matched by id (a rename is a change),
each changed field has a Spanish label and display text, the prescriber is
compared by id (its name in the snapshot is display only; a doctor merge
that repoints it is reported), and `sort_order` is never a field change — a
slot is "reordenado" only when the relative order of the items it kept
changed. A reorder is a change to the printed sheet, so it does create a
version. `source_entry_id` changes (e.g. nulled by a bitácora delete) stay
in the structured diff but not in the summary text. There is no database
check `end_date >= start_date`: removing a not-yet-started item ends it
yesterday, before its start (the API still rejects that order in input).

**Save rules** (`PUT /patients/{id}/meds`, `app/services/meds.py`): a
full-list save carrying every current item (with `id`; new ones without),
the EVITAR list, `expected_version` (the `version_no` the editor started
from, 0 = none) and an optional `change_summary`. In one transaction the
patient row is locked (`SELECT … FOR UPDATE`); a different current version
→ 409. Items are updated / created; current items missing from the request
are **removed by setting `end_date` = yesterday (Panamá)** so they leave
today's list but stay in the table; `sort_order` = position among the
request's items of the same slot. The new snapshot is diffed against the
**previous version's snapshot**, so an item whose end_date passed since the
last save appears as "se suspendió" in the next version. An empty diff →
no version, nothing written. `dry_run: true` returns the diff and the
auto-generated summary without saving (for the confirmation step). Every
row change and the version are audited.

`med_events` (derived history from before the app existed): patient_id,
event_date, kind (`inicio`, `suspension`, `cambio_dosis`, `sustitucion`),
medication, detail, source_entry_id, review_status (`ok` /
`pending_review`, like `log_entries`), source_index. Reconstructed by AI from
the bitácora; shown in the history timeline alongside versions. The
operator-run backfill (`python -m scripts.med_events backfill`, rules in
`docs/import.md`) creates one `pending_review` row per event of an imported
entry's `origin_ref.medication_events`, keyed by `(source_entry_id,
source_index)` (unique, migration 0014), so re-runs skip what exists. Any
member reviews them: `PATCH /patients/{id}/meds/events/{eid}` edits
(date, kind, medication, detail) and/or confirms (`review_status: ok`);
`DELETE` discards (hard delete, the audit row keeps the event and the
backfill never recreates it); `POST …/meds/events/confirm {ids}` confirms
the listed pending events at once («Confirmar todos»). All audited.
`GET …/meds/events` is keyset-paginated (newest first by `(event_date, id)`,
`{items, next_cursor}`, «Cargar más» in Historial); `?review_status=
pending_review` returns every pending event unpaged, so «Por revisar» never
misses one.

**LEDD** (Levodopa Equivalent Daily Dose, ticket #133) is **computed on read**
from a list's items (`app/services/ledd.py`, pure, versioned reference table
`TABLE_VERSION` with a source per entry: Tomlinson 2010 as the base, Jost 2023
for drugs Tomlinson lacks), so every version is measured with the same table.
It is returned as `ledd` on `GET /patients/{id}/meds` and on a version's
detail, as `ledd_mg` on each version of the list, and as `ledd_change`
(`{before_mg, after_mg, pct}`) on a save / dry run and a version's detail.
`ledd_substances` (migration 0029) holds medication names outside the table:
one row per normalized name (`ledd.name_key`), with Claude's proposal
(`is_dopaminergic`, `mechanism`, `factor`, `source`, `confidence`) and the
admin's decision (`queued` → `proposed` → `confirmed` / `rejected`, or
`failed`). A name with a row is never sent again.

### 5.5 Labs

| Table | Key fields |
|---|---|
| `lab_reports` | patient_id (nullable until identified), collected_on, reported_on, lab_name, attachment_id (the PDF), source (`whatsapp`, `upload`, `link`), source_message_id, status (`ok`, `needs_review`, `failed`), patient_match (how the patient was identified: name, CIP, manual), raw_extraction (jsonb) |
| `lab_results` | report_id, analyte_id, position, raw_name, value_num, comparator (`<`, `>`, `<=`, `>=`, null), value_text, unit, ref_low, ref_high, ref_text, flag (`alto`, `bajo`, `normal`, `anormal`, null), panel ("Hemograma", "Perfil tiroideo") |
| `analytes` | canonical_name ("Vitamina B12"), aliases (text[] — "Cobalamina", "B12", "VIT B12"), default_unit, category, specimen (`sangre`, `orina`, `heces`, `otro`), needs_review (created by the extraction from an unknown name) |
| `lab_extractions` | sha256 (unique), attachment_id, report_id, outcome (`lab_report`, `not_lab_report`, `failed`), error (short code), model, usage (token counts, cost) — one row per PDF content, so a file is sent to Claude at most once |
| `lab_links` | url, message_id, patient_id (guess), status (`pending`, `fetched`, `failed`, `ignored`) — lab-portal links shared instead of PDFs |

### 5.6 WhatsApp ingestion

| Table | Key fields |
|---|---|
| `whatsapp_imports` | uploaded_by, zip attachment, mode (`labs_only`, `backfill`, `daily`), range_from/range_to (detected), patient_focus (unused since #40: a backfill's patient is chosen per analysis run), status, stats (jsonb: messages new/dup, media, labs found…) |
| `whatsapp_messages` | import_id (first seen), sent_at, sender, text, forwarded, media_filename, attachment_id, media_import_id (the import that supplied the file), media_kind (`audio`, `image`, `pdf`, `video`, `other`), transcript, classification (jsonb), dedupe_hash (unique: sent_at truncated to the minute + sender + normalized text + a has-media flag, plus an occurrence number for identical repeats in one minute of one export), wa_key (migration 0023: the WhatsApp Web bubble `data-id` the sync extension matched when it uploaded the file; unique when set) |
| `suggestions` | kind (`create_entry`, `update_entry`, `create_appointment`, `med_change`, `lab_report`, `doctor`), patient_id, payload (jsonb, same shape as the target API), rationale, source_message_ids, status (`pending`, `approved`, `edited`, `rejected`), decided_by, decided_at, result_ref; also (migration 0019) source (`whatsapp`/`chat`), import_id, decision_note (rejection reason); patient_id null for a new doctor; (migration 0020) analysis_run_id (the backfill run that produced it); (migration 0021, #41) chat_session_id (the conversation a chat proposal came from) and created_by (the family member who asked; also the audit row's user) |
| `analysis_runs` | (migration 0020, §6.7 step 6) kind (`backfill`), patient_id (focus), date_from/date_to (Panamá days, inclusive), cross_check, reanalyze, status (`queued`, `waiting`, `running`, `done`, `stopped`; the API shows `failed` when its job gave up), stop_reason (`manual`, `cost_limit`), max_cost_usd, stats (jsonb: the analysis stats below + days_total, days_already_done), job_id (latest job), created_by, finished_at |

`dedupe_hash` hashes `sent_at` at **minute** precision (#45): phone exports
(the production baseline, up to 2026-10-08) have minutes only and WhatsApp
Web exports (everything from 2026-10-09 on) add seconds, so a Web export that
reaches back past that cutoff (no date range in its dialog) still dedupes
against the baseline; no stored hash changed, since every stored `sent_at`
had `:00` seconds. `sent_at` itself is stored with the export's seconds.

`dedupe_hash` deliberately leaves out the media file name and kind: the same
message must hash the same in an export *without* media (`<image omitted>`,
Android's generic `<Media omitted>`) and one *with* media (whose iOS file
names also carry a per-export sequence prefix), so a later with-media export
attaches its files to the messages already stored ("added to existing").
A document's descriptor (iOS/desktop `Informe.pdf • 2 páginas` before the
`<adjunto: …>` / `documento omitido` marker, or its name after
`<document omitted>` / an `<attached: …>` tag) only repeats the file and is
not part of the text; without media it gives the file name and kind. The
desktop (Web) *with-media* document forms have not been seen in a real
export: the parser assumes the iOS form or the name repeated after the tag.
System messages are not stored. Residual limits: sender names are as the
exporting phone saved them (an export from another sibling's phone, or after
a contact was renamed, does not dedupe), only iOS/Web exports carry seconds
(an Android export never dedupes against an iOS/Web one), and a desktop
with-media document form other than the assumed ones would store that
message twice.

`whatsapp_imports` also keeps the chunked-upload state (filename, size,
chunk_size/count, staging_key, upload_id), job_id and finished_at, and
(migration 0022, §6.8) `awaiting_media` / `media_completed_at` for sync
extension runs that upload files after the text; status is
`uploading`, `queued`, `processing`, `done`, `failed` or `cancelled`. Stats:
`messages` {total, new, duplicate, system_skipped}; `media` {uploaded,
added_to_existing, already_stored, skipped_mode, omitted, missing, too_large,
unsupported} → {kind: n}; unparsed_lines; unreferenced_files; `analysis`
(daily analysis, §6.7 step 4): {prompt_version, model, status, waits, days,
days_skipped, days_failed, days_done, suggestions_created, dropped_invalid,
skipped_duplicate, skipped_out_of_scope (backfill only), explicit_requests,
explicit_created, explicit_skipped, explicit_unanswered (#134), missing_media,
usage, cost_usd}.

`audio_transcripts` (migration 0017): one row per audio file **content**
(`sha256` unique) sent to the transcription provider, or skipped for good —
attachment_id, status (`done`, `failed`), text, error (short code:
`too_large`, `unsupported`, `rejected`), model, duration_s, usage (jsonb,
incl. the estimated `cost_usd`). The same voice note (forwarded, or in two
exports) is never sent twice: its text is copied to
`whatsapp_messages.transcript` of every message whose file has that hash.
Transient and configuration failures are not recorded, so they can be re-run.

`media_classifications` (migration 0018): one row per image/PDF file
**content** (`sha256` unique) from a `daily`/`backfill` import — attachment_id,
status (`done`, `failed`), method (`claude`; `sticker`: a WebP ≤ 512 KB whose header says square
and ≤ 512 px, as WhatsApp stickers are, no call; `lab_extraction`: content already read as a lab report, no call),
category (`lab_report`, `prescription`, `medical_document`, `other`),
description (≤ 200 chars, Spanish, non-identifying: document type,
specialty, counts — no names, ID numbers, birth dates), patient_hint (`papa`,
`mama`, `unknown`: only from what the document shows, e.g. the patient's
sex), error (short code: `too_large`, `unsupported`, `refusal`, `truncated`,
`invalid_json`, `api_error_4xx`), model, usage (token counts, `cost_usd`).
Copied to `whatsapp_messages.classification` of every message whose file has
that hash: `{status: "done", category, description, patient_hint, method,
sha256}` or `{status: "failed", error, sha256}`. Transient and configuration
failures are not recorded.

### 5.7 Chat

`chat_sessions` (title, created_by, last_message_at, generating_since — the
one-answer-at-a-time lock, stale after 30 min) and `chat_messages`
(session_id, role `user`/`assistant`, content jsonb — the exact Anthropic
content blocks: text, thinking, tool calls/results — author_user_id for a
family member's turn (tool results are `user` rows without author), status
`complete`/`interrupted`, meta jsonb — model, stop reason, token usage, error
code; created_at). One row per API message, so the history replays exactly
(migration 0016). Sessions are **shared** by all family members; each user
message shows who wrote it. Generated files are `attachments` owned by the
chat message (#33: the turn's final assistant message; their ids also go in
its `meta.attachment_ids`).

Archive and delete (#99, migration 0026): `chat_sessions.archived_at` hides a
session from the default list; «Archivadas» lists them and any member can
archive or restore (`PATCH /chat/sessions/{id} {archived}`, audited).
`DELETE /chat/sessions/{id}` is a hard delete for admins and the member who
started the conversation (it is "theirs" by authorship; admins clean up
anything), after a confirmation. Messages cascade; suggestions keep working
with `chat_session_id` NULL; generated files are unlinked when another
attachment row shares their stored object, otherwise their row is deleted and
the object removed from storage after the commit. The audit row keeps ids and
counts only, never the title or message text. Both refuse with 409 while a
turn is generating.

### 5.8 Jobs

`jobs`: kind, payload, status (`queued`, `running`, `done`, `failed`),
progress (0–100), message, attempts, locked_at, error; plus `max_attempts`
and `run_after` (retry backoff). Payloads hold ids only and messages/errors are
generic (no medical text). Lifecycle and handler rules: `app/services/jobs.py`.

## 6. Features

### 6.1 Bitácora

- **List** (mobile: cards; desktop: table) with filters: patient, doctor,
  type, date range, text search, "por revisar". Sort by date (default desc),
  doctor, type. Filters live in the URL (shareable, back-button safe).
- **Card** shows date, doctor/source, type chip, title, first lines of
  indications, attachment count.
- **Edit/create** in a bottom sheet (desktop: right drawer): all fields,
  doctor picker with "add new doctor", attachments (camera / gallery / file).
- **Detail** view with full text and attachment previews.
- Creating an entry can also create the **next appointment** ("Próxima cita")
  and propose **medication changes** (link to the meds editor).

**Médicos** (header menu, `/medicos`): directory with accent-insensitive
search on name/alias, specialty, clinic and a tap-to-call phone; add/edit in
a bottom sheet (aliases as chips, active/inactive). The doctor page
(`/medicos/:id`) shows contact info, aliases, notes and that doctor's
entries for the selected patient(s). Admins get **Combinar con…** to merge
a duplicate into the doctor (the importer creates some, e.g. "Dra. Pérez"
vs "Dra. Ana Pérez").

### 6.2 Bitácora import (one-time, from `.docx`)

1. Operator runs `python -m scripts.import_bitacora <file.docx> --patient papa`
   (`--dry-run` writes a JSON staging file only).
2. Convert DOCX → markdown keeping bold/underline; split into year sections
   (`BITACORA 2026` …) and entries by date lines (formats vary: `22 SEP 2026`,
   `09 JUL 26`, Spanish month abbreviations).
3. Each entry → Claude structured extraction: date, doctor (matched against
   / creating `doctors` with aliases), source_label, entry_type, channel,
   title, indications, family_notes (comments that are clearly the family's,
   e.g. "internet recomienda…"), **next appointment** (if any), **medication
   events** (start/stop/change/substitution).
4. Load as `review_status = pending_review`, origin `import_docx`. The
   original text of each entry, its next appointment and its medication
   events are kept in `origin_ref` (`original_markdown`, `next_appointment`,
   `medication_events`); the calendar and medications epics turn them into
   `appointments` and `med_events`. Operator procedure: `docs/import.md`.
5. The UI's "Por revisar" filter lets the family confirm entries over time;
   with it on, admins also get «Marcar todas como revisadas» (#58,
   `POST /log-entries/review`: every pending entry matching the list's
   current filters, one transaction, each entry audited).
   Re-running the import is idempotent (keyed on source position + hash).

### 6.3 Calendar

- **Agenda** (default on mobile; desktop opens on the month): upcoming
  appointments grouped by day («Hoy», «Mañana», sticky day headers); «Ver
  anteriores» loads past ones above, page by page. Cancelled ones stay,
  struck through with «Cancelada»; a past `programada` one shows «Sin
  confirmar» with a quick «Marcar como realizada». **Month** view (weeks
  start on Monday) with a dot per patient per day (Papá blue, Mamá orange,
  count when more than one; the day's aria-label says who and how many);
  tapping a day lists it below the grid. The patient switcher applies.
- Create/edit appointment sheet (patient, date, time or all day, duration,
  doctor, reason, place, notes; «Cancelar cita», «Volver a programar»,
  «Eliminar» for mistakes). Times are always Panamá time, whatever the
  device's time zone. **"Marcar como realizada"** opens a pre-filled new
  bitácora entry (patient, date, time, doctor, reason as title); saving it
  calls `POST /appointments/{id}/complete`, which creates the entry and
  marks the appointment `realizada` with `result_entry_id` atomically.
  Cancelling the sheet changes nothing.
- The **Bitácora** tab shows a «Próxima cita» card on top (next
  `programada` appointment of the selected patient(s); opens it in the
  calendar), hidden when there is none.
- **ICS feed**: each user gets a secret URL (`/api/v1/calendar/<token>.ics`)
  to subscribe in Google Calendar, Zoho Calendar, Apple Calendar or Outlook.
  Revocable/regenerable. Caveat shown in the UI: Google refreshes subscribed
  calendars every few hours. Screen «Calendario en tu teléfono»
  (`/calendario/suscribir`, from the header user menu and the Calendario
  header): only the token's SHA-256 is stored, so the URL is shown **once**,
  right after «Generar enlace» (copy button, `webcal://` link for Apple);
  afterwards it shows «Enlace activo desde <fecha>» with «Generar uno nuevo»
  (revokes the old URL) and «Desactivar», plus step-by-step instructions per
  app. The feed (`app/services/calendar_feed.py`) holds both patients'
  appointments from the last 6 months plus all future ones, every status:
  stable `UID` `appointment-<id>@app.example.com`, `SEQUENCE` growing with each
  edit, times with `TZID=America/Panama` + a fixed UTC-5 `VTIMEZONE`, all-day
  as `VALUE=DATE`, `STATUS:CANCELLED` (and a «Cancelada · » prefix) for
  cancelled ones, «✓ » for done ones. Since it lands in third-party calendars
  it carries only patient display name, doctor, reason, place and a link back
  to the app — **never** the free-text notes. Unknown, revoked and
  deactivated-user tokens all answer the same 404; responses are
  `Cache-Control: private, max-age=900` with an ETag.
- Optional per-user setting «Recibir invitaciones por correo»
  (`users.email_invites`, `GET/PATCH /me/settings`, toggle on «Calendario en
  tu teléfono»): an **email invite (.ics)** when an appointment is
  created/changed/cancelled/deleted, for instant updates
  (`app/services/calendar_invites.py`). Every appointment write (API,
  `/complete`, the bitácora's «Próxima cita») queues one `appointment_invite`
  job (ids-only payload) in its transaction, due 60 s later; further changes
  while it is queued reuse it (debounce). Not for the backfill script, notes-only
  edits, «Marcar como realizada», edits of a cancelled/done appointment, or
  appointments that have already ended. The job sends one message per active
  opted-in user: `multipart/mixed` → [`multipart/alternative` → text, HTML,
  `text/calendar; method=REQUEST|CANCEL`] + `invite.ics`, with the feed's
  `UID`/`SEQUENCE` (a deletion's CANCEL is built from the audit snapshot,
  `SEQUENCE` + 1), `ORGANIZER` = `EMAIL_FROM`, `ATTENDEE` = the recipient
  (`RSVP=FALSE`), `STATUS:CONFIRMED|CANCELLED`. Same content rules as the
  feed (never notes). Locally it is logged, not sent; in production without
  `RESEND_API_KEY` it is skipped with a warning; SMTP errors retry.

### 6.4 Medications

- Per patient, the list grouped by slot exactly like the printed sheet,
  plus "Otros medicamentos", "Rescate" and **EVITAR**.
- Edit item (bottom sheet), add, remove (sets end_date to yesterday), reorder within slot.
  Saving opens a confirmation with the auto-generated change summary
  ("Se agregó X; Y cambió de 1 a ½") which is stored with the version.
  Editing works on a working copy (nothing is saved until «Guardar»); the
  confirmation comes from a `dry_run` save, and a 409 (someone saved
  meanwhile) is shown, never merged or overwritten. With *Ambos* selected the
  tab stacks both patients' lists, each with its own «Editar». An empty list
  (no import yet) shows «Todavía no hay lista de medicamentos» and can be
  started by hand.
- **Historial** (`/medicamentos/historial`): timeline of versions
  (version, «Desde», who, summary, item count) merged with the derived
  `med_events` («De la bitácora» badge, kind, medication, detail, link to the
  source entry; `pending_review` ones marked «Por revisar»), newest first and
  grouped by month. A version sits on its «Desde» day. With *Ambos* both
  patients' timelines are stacked. Tap a version
  (`/medicamentos/historial/<n>?paciente=<id>`) to see the diff vs the
  previous one (words for each change — «Agregado», «Suspendido», «Cambió»
  field by field —, never color alone), move to the previous / next version,
  and «Ver lista completa de esta versión» (the snapshot as the printed
  sheet). Version 1 shows its list directly («Lista inicial»). Each
  bitácora event has «Confirmar» (pending only), «Editar» (bottom sheet:
  kind, medication, detail, date; saving a pending one confirms it) and
  «Descartar» (confirmation dialog). When a patient has pending events, a
  «Por revisar (N)» box tops the timeline: «Ver solo los por revisar»
  toggles a pending-only view, «Confirmar todos» (dialog with the count)
  confirms the ones shown.
- **Imprimir**: PDF matching the current Word layout (patient name + ID,
  "Desde <date>", sections by slot, separators, EVITAR, optional regimen table
  such as the Prolopa schedule). Letter, large legible type.
  `GET /patients/{id}/meds/print.pdf[?version=n]` (any member) prints the
  current list or a version's snapshot: «LISTA DE MEDICAMENTOS», the
  patient's `full_name` (else the display name), «CIP: …» only when
  `id_number` is set, «Desde <fecha larga>», the non-empty slots in sheet
  order (bold uppercase heading + rule; «Según necesidad (rescate)»), each
  item with the quantity big and bold (`1/2` → ½), name + strength + dose
  note, time · days · start/end dates · instructions, the schedule note and
  the prescriber; then the bordered **EVITAR** box and the `list_note`
  (markdown with tables, raw HTML shown as text). Footer «Impreso el <fecha>
  · Versión N» and «Página X de Y»; an item never splits across pages. The
  file name is `medicamentos-<slug>-<desde>.pdf` (no patient name: browser
  download history). Rendering lives in `app/services/pdf/` (shared
  `base.html` layout + one template per document + `render_pdf`), which
  the chat's `generate_pdf` reuses. In the app, «Imprimir / Descargar PDF»
  (per list; «Imprimir esta versión» on a version page) fetches the PDF
  with the session and then offers «Abrir PDF» and, where the phone
  supports sharing files, «Compartir o imprimir» (share sheet).
- **Checks** (warnings, never blocking; `app/services/meds_checks.py`): adding a medication that matches the
  patient's EVITAR list; bitácora entries newer than the last list version that
  mention medication changes not reflected in the list.
  - *EVITAR*: an item matches a row when one of the row's terms is a whole
    word/phrase of the item's name + strength (its last word may take a
    plural «s»/«es»), after normalizing (™ ℠ ® © as spaces, no accents,
    lowercase, punctuation as spaces). Terms: the substance, its parts split
    on `/ , ; ( ) +`, each one's leading words before the first word with a
    digit («Aspirina 100 mg» → «aspirina»), terms under 3 letters dropped,
    and their synonyms from
    a small built-in brand ↔ generic table (ibuprofeno/Advil/Motrin, ácido
    acetilsalicílico/Aspirina, acetaminofén/paracetamol/Tylenol/Panadol,
    naproxeno/Aleve, diclofenaco/Voltaren, metamizol/dipirona). No drug
    classes («AINEs» does not match «Ibuprofeno»). The item sheet warns as you
    type («Atención: X coincide con «Y» en la lista EVITAR (motivo)»), and
    every `PUT …/meds` response (the dry run too) carries `warnings`
    (`item_index`, `item_id`, `name`, `substance`, `reason`), shown at the top
    of the save confirmation. Copy: «Revisión básica por nombre; no reemplaza
    al médico.» The frontend (`lib/meds.js`) mirrors the backend rules; both
    run the shared case table `backend/tests/avoid_match_cases.json`
    (pytest, and `npm run check:meds` in `frontend/`). A missed match is the
    costly error, so the rules lean towards warning.
  - *Out of date*: `GET /patients/{id}/meds/checks` → `{reference_date,
    total, entries}`: the patient's entries dated **after** the current
    version's «Desde» (`effective_date`, else the Panamá day of `changed_at`;
    the imported version 1 is saved long after its sheet's date, and the
    changes in between are the ones that may be missing; an entry on the
    `effective_date` itself = reflected, but with the `changed_at` fallback
    an entry on the save's day still counts)
    with medication events: their `med_events` rows (any review status) plus
    the imported `origin_ref.medication_events` not yet backfilled nor
    discarded. Oldest first, at most 50. No version → nothing. A banner on
    Medicamentos («Hay N entradas de la bitácora posteriores a la versión
    actual que mencionan cambios de medicamentos») lists them with «Ver
    entrada» and «Ya está reflejado»: `POST …/meds/checks/{entry_id}/dismiss`
    stores a row in `med_check_dismissals` (one per entry, shared by the
    family, audited, idempotent). A dismissal is permanent per entry: events
    linked to that entry later do not bring it back. Manual entries have no extracted events, so
    they are not checked yet (a Claude check of new entries belongs to the
    daily ingestion of a later epic).
  - *LEDD* (ticket #133; reference only, the app never acts on it):
    «LEDD ≈ N mg/día» on Medicamentos for a list with any dopaminergic item,
    a «?» next to it (hover popover with a mouse, bottom sheet on a tap) with
    what each medication contributes (drug, daily dose, factor, mg, source),
    and a ◆ (with a text label) on every item that counts. Daily dose =
    active mg per unit (the levodopa content of a combination: Prolopa 250 =
    200 mg) × quantity (½, ¼, "2 cápsulas") × days/7. COMT inhibitors add
    levodopa LED × ratio; safinamide is a fixed 150. As-needed and
    conditional doses never count (family decision): listed apart as
    "+ hasta N mg si se da"; an item whose dose cannot be read is
    "no calculado", never guessed. The save sheet shows "LEDD: 920 → 1040
    mg/día (+13 %)" (no threshold alert, by decision); Historial shows each
    version's LEDD; the printed sheet carries it in the header; the chat's
    `get_current_meds` returns the breakdown and `get_med_history` each
    version's total. A name outside the table is classified once by Claude
    (`classify_substance` job after a save, `CLASSIFICATION_MODEL`, only the
    name and strength are sent) as a **proposal**: «pendiente de
    confirmar», never counted, until an admin confirms (optionally editing
    the factor) or says it does not count, from the breakdown sheet
    (`POST /ledd/substances/{id}/confirm|reject`, audited).
- Seed: one-time import of the two current Word lists via Claude extraction →
  version 1.

### 6.5 Labs

- **Initial load**: the operator exports the full WhatsApp group (with media)
  and uploads it with mode `labs_only`: only PDF attachments are processed;
  messages are stored (for later backfills) but not analyzed.
- Each PDF → Claude (PDF document input): is it a lab report? If yes, extract
  patient name/ID, lab, dates, and every result (raw name, value, unit,
  reference range, flag, panel). Patient matched by ID number or name against
  `patients`; unmatched → `needs_review`. Analytes matched against
  `analytes.aliases` (new names create analytes flagged for review). Same file
  (SHA-256) is never processed twice.
  - Implementation (`app/services/labs/`): a finished `labs_only` WhatsApp
    import queues one `lab_extract` job per new PDF (ids only in the
    payload); in `daily`/`backfill` imports PDFs and photos are classified
    first (§6.7 step 3) and only lab reports are queued (a photo as a
    single-image extraction keyed by its SHA-256, report source `whatsapp`);
    each job
    sends the PDF (≤ `LAB_PDF_MAX_MB`, default 15 MB: PDF input allows 32 MB
    per request and base64 adds a third) as a document block with a strict
    JSON schema. The outcome per SHA-256 is kept in `lab_extractions`,
    including "not a lab report" and permanent failures; transient API errors
    retry with the job's backoff.
  - Patient (a review is preferred to a wrong attribution; no match →
    `patient_id` NULL, `needs_review`). By ID number (digits/letters,
    separators and leading zeros ignored), unless the report's name has a word
    that patient's full name lacks: CSS forms for a dependent may print the
    insured relative's cédula; a one-word name confirms it only if no other
    patient's name has that word. Without an ID to compare, by name only when
    the report's name words (accent/case/order insensitive, particles
    dropped) **equal** the patient's full-name words — a subset, extra words
    (a son named after his father) or a tie → no match. A cédula-shaped ID
    that matches nobody → no match (an ID of another shape, e.g. a foreign
    lab's own number, is ignored). Then (#98) a **name variant**: the
    report's words (a "SURNAME, GIVEN" form reordered) appear **in order** in
    exactly one patient's name from its first given name (only words are
    missing; single letters are initials) **and** the name carries a surname
    the couple's children never carry: the patient's own 2nd surname, or
    the married "de <husband's surname>" with the "de" printed (surnames
    from `full_name` in cédula order "<given…> <1st> <2nd> [de <husband>]").
    Children are "<given> <father's 1st> <mother's 1st>" and may share their
    parents' given names, so "Juan Carlos Pérez" or "Ana María Pérez" never
    decide. Last, **Claude** (`CLASSIFICATION_MODEL`)
    sees only the printed name/ID and the patients' names; only a `high`
    confidence answer for a name with such a surname assigns, and the report
    stays `needs_review` for a person to confirm. The exact name rule needs
    such a surname too. A married "de X" counts only when X is the other
    patient's 1st surname; otherwise both readings stay and a possible 1st
    surname never distinguishes. `patient_match` records the method (`cip`,
    `name`, `name_variant`, `llm`, `manual`). `python -m scripts.lab_reports
    rematch [--dry-run]` re-runs this for unassigned reports from the stored
    extraction (`docs/import.md`).
  - Analytes are matched within the result's specimen (a urinalysis
    "Glucosa" is not blood glucose; an unknown specimen is `otro`, never
    blood); migration 0007 seeds 74 common ones.
  - Values: `value_text` keeps the printed value; `value_num` accepts `,` or
    `.` decimals and thousands separators; `<5` / `>100` keep the number and a
    `comparator`. A single `.`/`,` before exactly three digits (`250.000`,
    `4,500`) is ambiguous: the decimal separator the report's other numbers
    use decides, but if the result's reference range only fits the other
    reading (a unit mismatch, e.g. `4,500` /mm³ vs `4.0 - 10.0` ×10³) nothing
    is stored; with no such convention, the range alone decides when only
    one reading is plausible; otherwise `value_num` stays NULL and the report
    needs review. The lab's own flag (H/L/*) wins; otherwise a plain number
    is flagged against its numeric range (`bajo`/`normal`/`alto`), and
    bounded or text values, or values more than 30x off the range (likely a
    unit mismatch), get none.
  - Report `status`: `ok` when the patient is identified, there are results
    and no value was left ambiguous, else `needs_review`.
- **Lab portal links** in messages (URLs from known lab portals) are recorded
  in `lab_links`; the worker tries to fetch a PDF; failures stay `pending`
  so someone can download and upload the PDF manually.
  - Implementation (#16): an `after_import` hook records every URL in the
    import's new messages whose host matches `LAB_LINK_HOSTS` (comma-separated
    host globs, set by the operator) or whose path/query mentions
    `resultado(s)`, `laboratorio(s)`, `pdf` or `lab` (accents/case ignored),
    deduped on the normalized URL; `python -m scripts.lab_links scan` does
    the same for messages imported earlier. Patient: a guess only when the
    message names exactly one patient («papá» / «mamá»). Only `https` links on
    an allow-listed host are fetched (`lab_link_fetch` job, link id only in
    the payload); with the setting empty nothing is fetched automatically.
    The fetch treats the URL as untrusted: every redirect hop (≤ 3) must be
    `https`, allow-listed and resolve only to public addresses (no private,
    loopback, link-local/metadata, shared or reserved ranges, IPv6 forms
    included); the connection is pinned to the checked address; timeouts,
    no cookies, ≤ `LAB_PDF_MAX_MB`, and the bytes must start with `%PDF-`. A
    PDF is stored (SHA-256 dedupe), the link becomes `fetched` and the usual
    `lab_extract` job reads it (report `source = link`). A login page,
    another file, a 4xx or a blocked address leave it `pending` with a reason
    code; network errors/5xx retry with backoff, then stay `pending`.
    `failed` is not used: an unfetched link is always actionable. URLs may
    carry patient tokens: stored in the database only, never logged.
  - `/labs` shows «Enlaces pendientes (N)» (collapsible; the selected
    patient's links plus those without a guess): host, date, sender, guess
    and why it was not downloaded; «Abrir enlace» (new tab, `noopener
    noreferrer`), «Subir PDF» (the «Subir resultado» sheet with the link's
    id: the link becomes `fetched`, linked to the upload's file, job and,
    once read, its report; a portal with several PDFs takes them all at
    once, each read as its own report with source `link` while the link
    keeps the first, #124) and «Ignorar» (confirmed, audited). Any family
    member can act.
- **Screens**: list of reports by date (per patient, filter by test name or
  "fuera de rango"); report detail (results table with flags + PDF viewer);
  **analyte page** with a trend chart and a table over time with the reference
  range band.
  - Implementation (#14): `/labs` lists reports grouped by report date
    (`collected_on`, else `reported_on`, else the day stored), filters in the
    URL (`q` test name — raw name, analyte name or alias, substring, accent
    insensitive; `fuera=1`; `revisar=1` = `status=needs_review`; `desde`/
    `hasta`). Reports with no identified patient show under every patient
    selection with a «Paciente sin identificar» badge
    (`include_unassigned=true`) until someone assigns them. `/labs/:id` is
    the report (flags as icon + word + color, results by panel, PDF opened in
    a new tab or downloaded with a fresh presigned URL, a "Corregir" sheet
    for patient/dates/lab — a patient change is confirmed in a dialog and
    stored as `patient_match = manual`; "Marcar como revisado" needs a
    patient). `/labs/examenes/:analyteId?paciente=<id>` is the analyte page:
    one hand-rolled SVG chart per unit (mixed units never share an axis), the
    reference band steps with each date's own range, out-of-range points
    differ by shape (▲ alto, ▼ bajo, ◆ anormal) as well as color, `<`/`>`
    values are hollow at their bound, non-numeric values appear only in the
    table. Any family member can edit a report (§2), audited like every write.
- Manual upload of a lab PDF/photo from the phone goes through the same
  extraction.
  - Implementation (#15): «Subir resultado» on `/labs` (bottom sheet, any
    family member, as with every other record): one PDF, or up to 10 photos
    (camera or gallery, JPEG/PNG/WebP — HEIC is refused with a hint, Claude
    does not read it) of the pages of one report, uploaded in page order
    through `POST /attachments`, then `POST /lab-reports/extract`
    (`attachment_ids`, optional `patient_id`) queues one `lab_extract` job
    (payload: ids only). Photos go as `image` blocks in page order (≤ 5 MB
    each, ≤ `LAB_PDF_MAX_MB` together). Dedupe key in `lab_extractions.sha256`:
    a single file's SHA-256, or for several photos the SHA-256 of
    `"lab-pages:" + ",".join(ordered SHA-256s)` — the same pages in the same
    order are never read twice. The sheet shows the job's progress, then
    `GET /lab-reports/extract/{job_id}` gives the outcome and opens the report
    (or says "No parece un resultado de laboratorio"). Every page is linked to
    the report; the report page lists them.
  - Patient hint: never overrides the document. A CIP/name match to the other
    patient → no patient, `needs_review`; a match to the hinted one keeps its
    method. With no match, the hint assigns the patient (`patient_match =
    manual`) only when there is no comparable ID number and the printed name
    is absent or only words of the hinted patient's full name — and the
    report still stays `needs_review` until someone confirms it. The sheet
    starts at «No sé» (no hint), not at the header's patient. Files already
    owned by another record (e.g. a bitácora photo) are refused.
    The gallery input accepts only PDF/JPEG/PNG/WebP so iOS hands photos over
    as JPEG; a HEIC that still arrives is re-encoded to JPEG when the browser
    can decode it (`downscaleImage`, shared with the bitácora), else refused.
  - Analyte review (admins, header menu «Revisar exámenes»,
    `/labs/revisar-examenes`): the analytes flagged `needs_review` with their
    result count and example results. «Confirmar» clears the flag and may fix
    name/unit/category/specimen (a rename keeps the old name as an alias; a
    name another analyte already uses is refused — merge instead). «Combinar
    con…» folds it into an existing analyte like the doctor merge: rows
    locked, every FK to `analytes.id` re-pointed, canonical name and aliases
    merged (normalized dedupe), duplicate deleted, audited with the moved
    result ids; different specimens need an explicit confirmation.
  - Analyte triage (#100, `app/services/labs/triage.py`, prompt
    `app/prompts/analyte_triage_v1.py`): Claude (`EXTRACTION_MODEL`) gets the
    analytes under review in batches of 30 — name, aliases, specimen, units
    seen, three example values as printed, never a patient/date/lab — plus
    the reviewed catalogue (cached), and proposes per analyte `merge_into
    <id>` (reviewed targets only) / `keep_as_new` (+ cleaned name) /
    `unsure`, with confidence and reason code. Stored on
    `analytes.triage` (JSONB, migration 0027 — not a table with a FK, since
    the merge re-points every FK to `analytes`). Started by an admin on the
    page («Revisar con IA», estimated cost shown first; job
    `analyte_triage`, proposals only) or by the operator with
    `python -m scripts.analytes triage [--dry-run] [--apply]`; only
    `--apply` applies anything by itself: high-confidence keeps and
    high-confidence same-specimen merges whose units match (an analyte with
    no unit is never auto-merged: `units_match` null, #113). Each card shows
    the proposal with «Aceptar» / «Descartar»; «Aceptar todas las
    propuestas» applies the pending ones. Applying always goes through
    `merge_analytes` / `update_analyte` (same rules, audited); an analyte
    with a proposal (even a rejected one) is never sent again.
  - `/labs` shows «N informes por revisar» linking to the «Por revisar»
    filter; resolving them is #14's «Corregir» / «Marcar como revisado».

### 6.6 Chat

- claude.ai-like: session list (shared), new chat, streaming responses,
  markdown, each user message labeled with its author.
- Claude has **read-only tools** over the data:
  `search_log_entries`, `get_log_entry`, `list_appointments`,
  `get_current_meds`, `get_med_history`, `search_lab_results`,
  `get_lab_report`, `list_doctors`, `list_patients` (plus the file tools
  below and the ready-made `previsit_summary` / `emergency_card`).
- **File tools**: `generate_pdf` (title + sections of markdown/tables, using
  the app's print template) and `generate_xlsx` (sheets of rows). Files appear
  as download chips in the conversation and are stored in Spaces.
  Implementation (ticket #33): `generate_pdf {title, patient?, sections:
  [{heading?, markdown?, table?: {columns, rows}}], filename?}` renders
  `chat_document.html` (title, patient display name — never the CIP —,
  generation date in Panamá) through the WeasyPrint child process, holding
  no DB connection meanwhile; `generate_xlsx {title, sheets: [{name,
  columns, rows}], filename?}` builds the workbook with openpyxl (bold
  frozen header, autofilter, fitted column widths, ISO dates as date cells,
  Excel-valid unique sheet names, no cell ever a formula). Caps (50
  sections, 20k markdown characters per section and 60k in all, tables
  200 × 20, 10 sheets of ≤ 30 columns, 5,000 cells) answer with an error
  that asks Claude to shorten. The file is stored as an unlinked attachment
  (`documents/…`, `<slug>-<yyyy-mm-dd>.pdf|xlsx`), the stream sends a
  `file` event `{id, filename, mime, size}` (the UI shows the chip at
  once), and the turn's files are linked to its final assistant message —
  also when it is interrupted — in the same transaction that stores it. A
  turn that stores nothing (lost lock, storage error) leaves them unlinked.
- `propose_change` tool (#41) — the chat creates **suggestions** (e.g.
  "anota que hoy la Dra. Gómez…") that go to Pendientes; it never writes
  a record. Input `{kind, patient?, payload, rationale}`: `kind` is
  `create_entry` / `update_entry` / `create_appointment` / `med_change` /
  `doctor`; `payload` is the target API's body with the patient given as
  `patient` (slug or id, never `payload.patient_id`), an appointment as
  `date` + optional `time` (none = all day), `update_entry` text is
  append-only like #39 (`changes.add_indications` / `add_family_notes`,
  appended after the entry's full current text with the daily analysis'
  `entry_changes`, which skips text the field already has; a full
  `indications` / `family_notes` is refused, since the chat only sees
  excerpts), and a `med_change` gets the current list version as `base_version`.
  The tool validates like the WhatsApp producers (schema, producer guards,
  existing doctors, no future bitácora date), refuses duplicates of records
  or pending suggestions with the daily analysis' `find_duplicate`, then
  `create_suggestion(source="chat", chat_session_id, created_by=<asking
  member>)` and commits. Any problem comes back as an `is_error` tool result
  the model can correct. The stream sends a `suggestion` event `{id, kind,
  patient_id, summary}`; the ids go in the final assistant message's
  `meta.suggestion_ids`, and `GET /chat/sessions/{id}` lists them on that
  message as `suggestions` `[{id, kind, patient_id, summary, status}]`
  (current status). The UI shows each as a card linking to `/pendientes`;
  the Pendientes card says who asked and links back to the conversation.
  The system prompt asks Claude to propose what the family dictates (one
  proposal per change, ids looked up first, never "saved"). Adding the tool
  and that rule changed the cached prefix once: on their next turn, older
  sessions' replayed thinking blocks are dropped (`drop_block`) and the
  cache is rebuilt — no error, no data change.
- System prompt: answer only from the data, cite entry dates/doctors, say
  when something isn't recorded, no medical advice, Spanish by default.
  It is static (cached with the tools); each family message starts with a
  context line (author, date and time in Panamá) stored with it.
- Implementation (ticket #31, `app/services/chat/`): `POST
  /chat/sessions/{id}/messages` streams SSE (`start`, `delta`, `tool` with a
  Spanish status, `file` (#33), `suggestion` (#41), `error`, `message`, `title`, `done`; `: ping` heartbeats)
  from a background thread, so an answer completes even if the phone drops
  the connection; at most `CHAT_MAX_TOOL_ITERATIONS` tool rounds (then a
  final answer without tools); one answer at a time per session (409); no
  DB connection held while Claude generates; tool results capped at ~24k
  characters; history longer than ~300k characters drops its oldest turns
  with a note. The session title comes from the first exchange
  (`CHAT_TITLE_MODEL`, falling back to the question shortened).
  The lock is the turn's own timestamp and is cleared only by that turn
  (compare-and-clear); a turn stops starting requests or tools after 8 min,
  the lock counts as stale after 30 min (above the worst-case turn), and a
  turn that lost its lock stores nothing (`error` code `lock_lost`).
- Audit: creating a session and posting a family message are audited
  (`chat_session` / `chat_message`, `create`). Assistant messages, tool
  results and auto-titles are system writes and are not audited.
- For the UI (#32): after the final `message` event, re-render the answer
  from the stored message (`GET /chat/sessions/{id}`): with the refusal
  fallback, text streamed as `delta` may include a declined partial that
  the stored message omits. A session stuck at 409 after a worker restart
  frees itself after 30 min (`generating: true` until then).
- UI (ticket #32): shared session list (title or «Nueva conversación», last
  activity, who started it) and a conversation view: prompts labeled with
  their author («Tú» for oneself), answers in markdown (GFM, no raw HTML)
  with the tools they used, live tool status while streaming, interrupted
  answers marked «Respuesta incompleta» with «Reintentar» (resends the
  prompt as a new message). A dropped stream shows a notice and the page
  re-reads the session until `generating` is false. Generated files come
  in each assistant message's `attachments` (`id`, `filename`, `mime`,
  `size`: attachments owned by that `chat_message`) and show as download
  chips.
- Privacy: tools expose patients' display names, diagnoses and notes, never
  `full_name`, `id_number` or `birth_date` (the emergency card, #34, reads
  those in its own code path). Message text, tool inputs and tool results
  are never logged.
- Ready-made actions (also as buttons outside chat): **Resumen pre-consulta**
  (everything since the last visit with doctor X: entries, med changes,
  labs) and **Tarjeta de emergencia** (diagnoses, current meds, EVITAR,
  doctors, ID). Implementation (ticket #34):
  - **Resumen pre-consulta** (`app/services/previsit.py`,
    `GET /patients/{id}/previsit.pdf?doctor_id=`, chat tool
    `previsit_summary {patient, doctor}` — doctor by id or name): the
    period runs from the **last visit** — the patient's latest entry with
    that doctor of a visit type (`consulta_control`, `consulta_enfermedad`,
    `visita_domiciliaria`, `teleconsulta`) dated before today — through
    today, both inclusive; with no earlier visit, the last 12 months. It
    lists the period's entries of **every** doctor (oldest first, at most
    100, the doctor's own marked «ESTE MÉDICO»), medication changes (list
    versions dated in the period — `effective_date`, else the day saved —
    with their summary, and `med_events` dated in it), the current list +
    EVITAR, the period's lab reports (not `failed`; the newest 30, the
    PDF says «Se muestran los 30 más recientes de N» when capped) with
    every result —
    out-of-range ones bold **and** labelled «ALTO» / «BAJO» / «ANORMAL» —,
    upcoming `programada` appointments (all doctors, that doctor's marked)
    and 12 blank lines «Preguntas para el doctor». The chat tool's counts
    (`lab_reports`, `out_of_range_results`) cover the whole period, shown or
    not. It names the patient by
    display name only. Buttons: the doctor page (`/medicos/:id`, the
    patient chosen there when the switcher says «Ambos») and a
    `programada` appointment with a doctor in the calendar sheet.
  - **Tarjeta de emergencia** (`app/services/emergency_card.py`,
    `GET /patients/{id}/emergency-card.pdf`, `GET
    /patients/{id}/emergency-card` (JSON for the screen), chat tool
    `emergency_card {patient}`): full name (else display name), CIP, birth
    date and age, diagnoses, today's medications (one line per
    medication, its slots merged: «Medicina 50 mg — ½ Desayuno; ½ Cena»;
    each dose carries its weekdays, schedule note and short instructions
    («1 Desayuno — lun (semanal)»), identical rows stay separate doses, a
    row without quantity says «cantidad según indicación»;
    never truncated — a long list flows to a second page after everything
    else), EVITAR (boxed, first after the name) and up to 8 active
    doctors that appear in the patient's entries, non-cancelled
    appointments or as prescriber of a current medication, most recent
    reference first. One Letter page for a typical list, «actualizada el <fecha>» = the day it
    is generated (it is always built from current data). There is no
    emergency-contact field, so the card has none. Screen: `/emergencia`
    (header menu): big type, EVITAR first, tap-to-call phones,
    «Descargar PDF»; it needs a connection — the service worker never
    caches `/api` — so the PDF is the offline copy.
  - **Privacy decision**: the card is the one chat-reachable document that
    carries `full_name`, `id_number` and `birth_date`. The tool renders it
    server-side and stores it as the family's own chat attachment (like
    #33); the tool result Claude sees has only `file_id`, the file name
    (`tarjeta-emergencia-<slug>-<fecha>.pdf`) and counts, so the private
    fields never enter the model's context, the stored history or the logs.
    `previsit_summary` likewise returns counts, the period dates and the
    doctor id. Both PDFs: `Content-Disposition: inline` with slug + date
    (no names), `Cache-Control: no-store`, rendered in the WeasyPrint child
    process after the database connection is given back (503 if it fails).

### 6.7 Daily WhatsApp ingestion and Pendientes

1. Someone exports the group for a date range (WhatsApp Web supports
   **Export chat → Custom date range**, with media) and uploads the zip
   (mode `daily`). Later the browser extension does this automatically.
2. Parse → dedupe against stored messages (overlapping ranges are harmless) →
   store new messages + media.
3. Process media: audio (`.opus`) → transcription; PDF/images → classify
   (lab report, prescription/Rx, medical document, other) — lab reports go
   through the labs pipeline.
   **Transcription** (#36, `app/services/transcription/`): an `after_import`
   hook (`daily`/`backfill` only) queues one `transcribe_audio` job per new
   audio hash (ids-only payload; a hash already in `audio_transcripts` is
   copied at once). The job sends the file to OpenAI `/v1/audio/transcriptions`
   (`gpt-transcribe`, `languages[]=es`, a fixed generic Spanish prompt and
   `keywords[]`: names on both patients' current medication lists, then active
   doctors' names, ≤ 400 characters — nothing else). WhatsApp voice notes are
   Ogg Opus, which the API accepts as `.ogg`: no ffmpeg. Raw AAC is not
   accepted and is recorded `unsupported`; files over `TRANSCRIPTION_MAX_MB`
   (25, the API limit) `too_large`; a 400/413/415 `rejected`. Timeouts,
   network errors, 429 and 5xx retry with the job backoff; a missing or
   refused key fails the job at once. Logs: ids, sizes, durations, cost —
   never transcript text. The UI piece is
   `frontend/src/components/media/AudioMessage.jsx` (player with the
   presigned URL fetched on play + «Transcripción automática», pending /
   unavailable states), used by the Pendientes screens.
   **Classification** (#37, `app/services/media_classification/`): an
   `after_import` hook (`daily`/`backfill` only) queues one `classify_media`
   job per new image/PDF hash (ids-only payload; a hash already in
   `media_classifications` is copied at once). The labs hook skips these
   modes, so each file costs one cheap call (`CLASSIFICATION_MODEL`, effort
   `low`, strict JSON schema `{category, description, patient_hint}`, image or
   PDF content block; images ≤ 5 MB, PDFs ≤ `LAB_PDF_MAX_MB`, HEIC
   `unsupported`) and only lab reports go on to `lab_extract` with
   `EXTRACTION_MODEL` — at most once per content (`lab_extractions` checked
   first). Stickers (WebP ≤ 512 KB, square and ≤ 512 px per the header, read
   without decoding) and content already read as a lab report are decided
   without a call. A classification that fails for good for any reason but
   `too_large`/`unsupported` (refusal, invalid output, a 400…) still queues
   `lab_extract`, so no lab report is lost to the cheap model. Retries,
   permanent failures and logs as for transcription (ids, category, token counts and cost; never the
   description). Known limit: a report photographed page by page in separate
   messages is extracted one page at a time.
4. Group new messages by day; Claude gets them (text + transcripts +
   image/PDF content + current context: patients, doctors, current meds,
   recent entries, upcoming appointments) and proposes **suggestions**:
   new/updated bitácora entries, appointments, medication changes, new
   doctors. Irrelevant chatter produces nothing.
   **Implementation** (#39, `app/services/daily_analysis/`, prompt and schema
   versioned in `app/prompts/daily_analysis_v1.py`, today
   `daily_analysis_v2.py`): an `after_import` hook
   (`daily` only, when the import stored new messages) queues an
   `analyze_import` job delayed 2 minutes. Transcripts and classifications
   come from other jobs, so the job first checks the import's new
   audio/image/PDF messages: while one has no result (nor a recorded
   failure) **and** its `transcribe_audio`/`classify_media` job is still
   queued or running, it queues itself again 3 minutes later — at most 10
   times (≈ 30 min); then, or when nothing is pending, it goes ahead and
   tells the model how many files of the day are missing (those days'
   suggestions say so in their rationale). New messages (first stored by
   this import) are grouped by Panamá day; a day with no text, transcript or
   medical classification is skipped without a call; the others go one call
   per day (chunks of ≤ 40 000 characters for very long days) to
   `ANALYSIS_MODEL` (default `claude-opus-5-5`: the medical judgement — a
   real medication change vs. a comment, a duplicate vs. a new visit —
   matters more than price at a few calls per day), effort `medium`, with a
   cached static prompt and a cached record context built once per job
   (patients + diagnoses; active doctors with ids and aliases; current
   medications with item ids; per patient the last 30 bitácora entries plus
   those within 7 days of the analyzed days; scheduled appointments from the
   first analyzed day). Images and PDFs are described by their
   classification (description, category, patient hint), **not resent** as
   content: the classifier already read them once and lab reports go
   through the labs pipeline. The answer is a strict JSON schema (one object
   shape per kind; "" / 0 for "none"). Each proposal must cite messages of
   that day, is validated with the target schema, and is skipped as a
   duplicate when the patient already has an entry that date with the same
   doctor, the same entry type and a similar title (with a doctor:
   normalized `difflib` ≥ 0.6 or containment; without one: both titles
   present and, after their common leading words, ≥ 0.85 — a near-duplicate
   in Pendientes is cheaper than a new event silently skipped),
   an appointment within ±1 h (same day for all-day) with the same doctor, a
   current medication with the same name (add), a doctor with that name or
   alias, or a pending suggestion of the same kind for the same
   date/doctor/entry/medication. The prompt forbids invented dates and
   doses, allows med changes only when a doctor prescribed them or the
   family decided them explicitly (stop/change by the current item's id and
   exact name), and prefers a bitácora note when unsure. An `update_entry`
   from the analysis only **appends**: the model sees a short summary of each
   entry, so it sends only the new text (`add_indications`,
   `add_family_notes`) and the code appends it to the field's full current
   text; a title only fills an empty one; nothing new → dropped. Each day
   takes a per-import advisory lock (a second job for the same import
   re-queues itself) and commits with `stats.analysis` (`days_done` makes
   reruns idempotent); progress is reported before every call; at most
   `ANALYSIS_MAX_DAYS_PER_JOB` (14) days per job, the rest in a follow-up
   job. Missing key or a refused key/model fails the job, as does a
   400/413/422 before any call of the job succeeded (a broken request);
   429/5xx retry; a refusal, an invalid answer or a later 400 counts the day
   as failed. Logs: ids and counts.
   **Explicit requests** (#134, prompt v2): family members address the app
   with messages starting with «Bot» («Bot: … reflejar en la lista de
   medicamentos y bitácora», «Bot corrección … LISTA DE MEDICAMENTOS: X se
   reemplaza con Y»). The code detects them (`pipeline.is_bot_request`: the
   first word is «Bot», case/accent-insensitive, after spaces, quotes or
   WhatsApp formatting marks; never «Bote» or «robot») and tags their line
   `[PEDIDO EXPLÍCITO]`; the prompt says to always produce what such a
   message asks (a list change is a `med_change` even without a doctor, never
   just a bitácora note), copy its wording, express «X se reemplaza con Y»
   as a `stop` of X + an `add` of Y (the payload has no `replace`), keep the
   named patient and, when the change is already reflected in that patient's
   list, say so instead of inventing one. Every suggestion citing a tagged
   message gets the rationale prefix «Pedido explícito: » (code, not the
   model); Pendientes lists those first with a «Pedido explícito» badge. The
   usual validation and duplicate rules still apply (an `add` of a name
   already on the list is the "already reflected" case); tagged proposals
   they skip and requests no created suggestion cites are counted in
   `stats.analysis` (`explicit_requests`, `explicit_created`,
   `explicit_skipped`, `explicit_unanswered`) and logged as counts.
5. **Pendientes** inbox: each suggestion shows the proposed record, the
   rationale, and the source messages (play audio, view photo). Actions:
   Aprobar / Editar y aprobar / Rechazar. Approval writes the record (audit
   logged) with origin `whatsapp`; source media are attached to it.
   **Implementation** (#38, `app/services/suggestions.py`,
   `app/api/suggestions.py`): producers (#39, #41) call
   `create_suggestion(db, kind=…, payload=…, rationale=…,
   source_message_ids=…, source=…, patient_id=…, import_id=…)` (the chat
   also passes `chat_session_id` and `created_by`), which
   validates the payload with the target API's schema
   (`app/schemas/suggestions.py`: `LogEntryCreate`; `{entry_id, changes:
   LogEntryUpdate}`; `AppointmentCreate`; `MedChangePayload` `{action:
   add|stop|change, medication, item_id?, item?, base_version?}`;
   `DoctorCreate`); producers may not set `attachment_ids`, and a
   `med_change` stop/change must name its target item (normalized name,
   strength ignored; re-checked at approval → 422). `lab_report` is reserved: labs extract themselves, so it
   has no producer and no apply path (approve → 422; reject works). Any
   signed-in member decides (§2). Approve locks the row, 409 if already
   decided, applies through the normal services (same validation, audit rows
   under the approving user) and records status/decided_by/at/result_ref in
   one transaction; a validation failure answers 422 with the service's
   message and leaves it pending. A body `payload` → status `edited` (the
   original proposal is kept; the audit row has `applied_payload`). New
   entries get origin `whatsapp`/`chat` and `origin_ref {suggestion_id,
   message_ids}`. Source files (audio, image, PDF; not video) are attached to
   the new/updated entry or the new appointment as **new attachment rows on
   the same stored object** (`attachments.copy_attachments`): an attachment
   has one owner, so re-linking would take it from its message. A
   `med_change` is a full-list save built from the **current** list plus the
   one change (`POST /suggestions/{id}/preview` is its dry run; approve takes
   the previewed `expected_version`, 409 if the list was saved since); a
   stale `base_version` is flagged but still applied to the current list;
   an item no longer on the list → 422. A `doctor` creates the doctor (409
   naming an existing match) or, with `doctor_id`, links to an existing one.
   UI: header badge (`GET /suggestions/count`, polled every minute and on
   focus) → `/pendientes`, one card per suggestion; «Editar y aprobar» opens
   the normal `EntrySheet` / `AppointmentSheet` / `DoctorSheet` pre-filled
   (`prefill` + `submit` props), and for medications `MedItemSheet` then the
   editor's `SaveSheet` diff.
6. **Backfill mode** for a date range and patient focus — used once to build
   Mamá's bitácora from 2026 messages, and optionally to cross-check Papá's
   2026 entries (suggest only what's missing).
   **Implementation** (#40, `app/services/daily_analysis/backfill.py`, prompt
   `app/prompts/backfill_analysis_v1.py`, today `backfill_analysis_v2.py` with
   the explicit requests of step 4, API `app/api/analysis_runs.py`, UI
   «Análisis histórico» on Importar WhatsApp; operator procedure in
   `docs/import.md`). Uploading a `backfill` import only stores messages and
   media and runs transcription/classification (step 3); it never analyzes.
   An admin then starts an **analysis run** (`analysis_runs`): patient, date
   range (≤ `BACKFILL_MAX_RANGE_DAYS`, not after today), cross-check,
   reanalyze, cost limit. The run reads **every stored message of the
   range, whatever import stored it** (an export whose messages were already
   stored by an earlier import adds no new messages, so "new messages of
   the import" would be empty). A cost estimate comes first (no call: the
   rendered messages and each job's real context are measured, ~2 000
   output tokens per call assumed). The `analyze_backfill` job is the step 4
   pipeline with: the backfill system prompt (same schema), the run's
   instructions (patient, cross-check, today, the medication list's
   «Desde») above the record context, today's medication list and
   appointments from today in the context, "MENSAJES del …" day headers,
   and code rules — proposals for the other patient, past appointments,
   medication changes from a day up to «Desde» and, in a cross-check,
   `update_entry`/`med_change` are dropped (`skipped_out_of_scope`); a
   rejected suggestion counts as a duplicate like a pending one; a
   cross-check also treats an entry with the same doctor and type ±2 days
   away as the same visit. It waits for media still being processed up to
   40 × 3 min, analyzes `ANALYSIS_MAX_DAYS_PER_JOB` days per job (per-run
   advisory lock, `stats.days_done`, progress "Analizando día n de N"), and
   stops before a day once `stats.cost_usd` reaches the run's
   `max_cost_usd` (`BACKFILL_MAX_COST_USD`, default 20) or an admin pressed
   «Detener»; «Reanudar» (with a higher limit after a cost stop) continues.
   Days an earlier run with the same patient and cross-check analyzed are
   skipped unless `reanalyze`. «Rechazar sus pendientes» rejects the run's
   pending suggestions (audited), the undo. Suggestions carry
   `analysis_run_id` (no `import_id`).

### 6.8 WhatsApp Web extension

Chrome MV3 extension (same approach as `pic-extension`), in `extension/` in
this repo. Runs only in the operator's own browser, only when clicked. DOM
recon: `docs/whatsapp-web-recon.md` (#44: export text + per-message media,
not DOM scraping); WhatsApp Web changes often, so selectors are isolated in
one module. Precision mismatch: the stored baseline (a phone export) and
the Web DOM have minutes only, a Web export has seconds, and `dedupe_hash`
hashed `sent_at` exactly — so #45 truncates it to the minute inside
`dedupe_hash` (no stored hash changes, recon §4a). The phone export is a
one-time baseline with a clean cutoff (everything up to 2026-10-08; from
2026-10-09 everything comes from WhatsApp Web); the truncation is the safety
net for a Web export that reaches back past that cutoff.

WhatsApp Web's **Export chat** has **no include-media option**: the export
is text only, every file `<… omitted>`. So a run ("Sincronizar") has two
halves (the operator's direction on #44):

1. asks the app for the newest stored message (`GET /imports/whatsapp/last`),
   exports the chat text and uploads it (wrapped in a zip) as a `daily`
   import with `expects_media: true` (the normal chunked upload);
2. once that import is `done`, lists the stored messages that still lack
   their file (`GET /imports/whatsapp/media-missing`), opens a few of them in
   WhatsApp Web, downloads each and uploads it for its **message id**
   (`POST /imports/whatsapp/{run}/messages/{id}/media`, with the bubble's
   `data-id` as `wa_key`), then says it is done
   (`POST /imports/whatsapp/{run}/media/complete`). Several runs a day each
   handle a few files. Files a run could not get (more than its cap, a
   bubble not found or ambiguous, the 90-minute window over) are retried by
   later runs: each run also lists the earlier runs' missing files of the
   last 14 days (`since`, uploaded through the current run), and the
   extension keeps a small local *skip book* of message ids it failed on
   (retried after 2 days; server refusals not within the window).

The extension (`extension/`, README there): popup «Sincronizar» + options
(API base, key, group name, dry run). It finds a bubble by day (separators),
minute, sender, kind and caption; `media-missing` groups *twins* by the
minute, sender, caption and media kind, and the extension skips them unless
exactly that many bubbles of that kind match (and skips when a row of that
day is unreadable).
Files are captured in memory by a MAIN-world script that intercepts the
page's save (blob anchor / `showSaveFilePicker`), never read from disk; a
capture left unanswered quarantines the page (every save swallowed, every
capture refused) until the tab is reloaded, and each sync starts by
reloading the WhatsApp Web tab, so a late file is never attributed to
another message. The page acknowledges each arm and nothing that can save
is clicked before that ack (WhatsApp saves a voice note it already holds
in the same task as the click); no ack → the capture is disarmed and the
run stops. The export uses the dialog's custom date range, from the Panamá
day of the newest stored message to today (inputs found by `aria-label`);
with nothing stored, all messages. Every
selector and label is in `extension/src/content/selectors.js`; an unexpected
UI state stops the run with a Spanish status. The extension's key cannot
read «Pendientes», so the popup links there.

Files reach the same pipeline as files inside an export: the
`whatsapp_media` job runs the `after_media` hooks (transcription,
classification → lab extraction) for the run's import (`media_import_id`).
A run the extension never completes is finalized by the server (a deadline
job 90 min after its import finished, its daily analysis at that bound, or
the same admin's next run starting / completing); uploads to a finalized or
expired run are refused. For a run, the media hooks run only at that point
(never also at the end of its import), so no file is handed over twice.
The daily analysis of the run waits while `awaiting_media` (at most 90 min
after the text import finished), so it reads the transcripts and
descriptions of files uploaded in the same run; a file for a message an
earlier run stored is processed but its day is not analyzed again.

**Auth**: a per-admin, revocable API token generated in the app («Importar
WhatsApp» → «Extensión de Chrome»), sent as `Authorization: Bearer`; only its
SHA-256 is stored (shown once, like the calendar feed), it opens only the
`/imports/whatsapp` endpoints and only while its user is an active admin. It
was chosen over capturing the session: the session cookie is `HttpOnly` +
`SameSite=Lax` and stays that way. CORS is allowed only for the configured
`EXTENSION_ORIGINS` (`chrome-extension://<id>`, empty = off), never with
credentials; the CSRF header rule still applies. Contract for the extension:
`docs/extension-api.md`.

## 7. API outline

REST under `/api/v1`, JSON, cookie auth (except the ICS feed, health, and
the sync extension's bearer token on `/imports/whatsapp`, §6.8).

```
POST /auth/request-link      POST /auth/verify     POST /auth/logout   GET /auth/me
GET  /patients               GET/POST/PATCH /doctors   POST /doctors/{id}/merge (admin)
GET/POST /log-entries        GET/PATCH/DELETE /log-entries/{id}   POST /log-entries/review (admin, bulk)
POST /attachments (multipart)  GET /attachments/{id}/url
GET/POST /appointments       GET/PATCH/DELETE /appointments/{id}
POST /appointments/{id}/complete (new entry + realizada, atomic)
GET  /calendar/{token}.ics (no cookie)   GET/POST/DELETE /calendar/feed (status / regenerate / revoke)
GET/PATCH /me/settings (own settings: email_invites)
GET  /patients/{id}/meds     PUT /patients/{id}/meds (save → new version)
GET  /patients/{id}/meds/versions[/{n}] (list without snapshots / snapshot + diff vs previous)
GET  /patients/{id}/meds/events?limit=&cursor=[&review_status=pending_review] (derived history, newest first, keyset pages)
GET  /patients/{id}/meds/print.pdf
GET  /patients/{id}/previsit.pdf?doctor_id=   GET /patients/{id}/emergency-card[.pdf]
GET  /patients/{id}/meds/checks   POST /patients/{id}/meds/checks/{entry_id}/dismiss
GET  /lab-reports            GET/PATCH /lab-reports/{id}   GET /analytes/{id}/series?patient=
POST /lab-reports/extract    GET /lab-reports/extract/{job_id}   GET /analytes[?needs_review=]
PATCH /analytes/{id} (admin)  POST /analytes/{id}/merge (admin)
POST /imports/whatsapp (start a chunked zip upload; admin)   GET /imports/whatsapp[/{id}]
PUT  /imports/whatsapp/{id}/chunks/{i}    POST /imports/whatsapp/{id}/complete
DELETE /imports/whatsapp/{id} (cancel an unfinished upload)
GET  /imports/whatsapp/last   GET /imports/whatsapp/media-missing (sync extension, §6.8)
POST /imports/whatsapp/{id}/messages/{message_id}/media   POST /imports/whatsapp/{id}/media/complete
GET/POST/DELETE /extension/token (admin, cookie only: status / regenerate / revoke)
POST /analysis-runs/estimate   GET/POST /analysis-runs   GET /analysis-runs/{id} (admin)
POST /analysis-runs/{id}/stop|resume|reject-pending (admin)
GET  /suggestions[?status=&patient_id=]   GET /suggestions/count
POST /suggestions/{id}/approve|reject   POST /suggestions/{id}/preview (med_change dry run)
GET/POST /chat/sessions      GET /chat/sessions/{id}
POST /chat/sessions/{id}/messages (SSE stream)
GET  /jobs/{id}              GET /health
```

## 8. Security and privacy

- Private GitHub repo; **no PHI in git** (`.gitignore` covers `*.docx`,
  `*.zip`, `private/`, `exports/`). Synthetic fixtures only.
- Spaces bucket private; presigned URLs only; bucket versioning on (enabled
  2026-10-08).
- Managed Postgres daily backups (DO) + weekly logical dump to Spaces.
- Data processors: **Anthropic** (extraction, classification, chat;
  commercial API terms, not used for training by default) and **OpenAI**,
  for **audio only**: family WhatsApp voice notes are sent to OpenAI's
  transcription API (API data is not used for training by default), with a
  generic prompt and medication/doctor names as spelling hints — no other
  record data. Accepted by the operator on behalf of the family (2026-10).
- Cookies `HttpOnly`/`Secure`; CSRF: `SameSite=Lax` + require a custom header
  (`X-Requested-With`) on state-changing requests. The only CORS is for the
  sync extension's configured origin, without credentials (§6.8).
- Rate limits on auth endpoints; generic error messages.

## 9. Estimated running cost

Droplet/DB/Spaces already exist (incremental: one small database + storage,
a few USD/month). AI: initial imports (≈400 entries + a few hundred PDFs) on
the order of USD 10–30 once; daily use and chat a few USD/month.
Transcription: cents per day.

## 10. Phases → epics

1. **Foundation & Bitácora** — scaffold, CI, deploy, auth, patients/doctors,
   attachments, bitácora CRUD + UI, DOCX import.
2. **Labs** — WhatsApp export parser + upload + worker, lab extraction,
   labs UI, manual upload, lab links.
3. **Calendar** — appointments, calendar UI, ICS feed, email invites.
4. **Medications** — model + seed import, editor, versions/diff, print PDF,
   derived history, checks.
5. **Chat** — tool-use backend, UI, PDF/XLSX generation, pre-visit summary and
   emergency card.
6. **WhatsApp daily ingestion** — transcription, media classification,
   suggestions + Pendientes, daily analysis, Mamá 2026 backfill.
7. **WhatsApp Web extension**.

## 11. Open decisions

Decided: transcription provider — OpenAI `gpt-transcribe` ($0.0045/min,
language and keyword hints), operator decision for #36; self-hosted
faster-whisper was not pursued.

- Whether approved WhatsApp suggestions may ever auto-apply (proposal: never
  in v1).
