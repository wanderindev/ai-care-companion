# n16chat - Project Constitution

## Project Overview

n16chat is a private family web app that keeps two elderly parents' medical
information in one shared place: the visit log (*bitácora*), appointments,
medication lists, lab results, and an AI chat over all of it. A handful of
siblings use it, mostly from phones. `docs/DESIGN.md` is the source of truth
for the data model, UI principles and privacy rules; `docs/deploy.md` is the
infrastructure inventory.

- **UI language: Spanish only.** Code, identifiers, comments and docs are in English.
- **Mobile-first PWA**: large touch targets, bottom navigation, bottom sheets
  instead of side drawers on phones. Desktop is secondary.
- Patients are always referred to as **Papá** / **Mamá** (slugs `papa`, `mama`).

## Privacy Rule (no PHI in git)

**Never commit real patient data.** No source `.docx` files, WhatsApp exports,
lab PDFs, real names, ID numbers (CIP), birth dates or medical text from the
real records. Tests, fixtures, seeds and examples use **synthetic data only**.
The real source files live only on the operator's machine (`.gitignore` covers
`*.docx`, `*.zip`, `private/`, `exports/`) and in production storage. Logs and
error reports never contain medical content. If you spot PHI in a diff, stop
and report it instead of committing.

## Architecture

```
React 19 + Vite + Tailwind v4 (PWA, react-router, TanStack Query)
        │  same origin, /api/v1, cookie session
FastAPI (Python 3.12, SQLAlchemy 2.0 sync, Alembic, Pydantic v2)
        │
PostgreSQL 17 (DO managed in prod; docker locally)
```

Attachments live in DO Spaces (private bucket, boto3; MinIO locally). A worker
process (same image) runs background jobs from the `jobs` table; later epics
add more Anthropic API use; voice notes are transcribed with OpenAI
(`OPENAI_API_KEY`, audio only); magic-link email goes through Resend over SMTP
(`RESEND_API_KEY`); see `docs/DESIGN.md` §4.

**Monorepo structure:**
- `backend/` - FastAPI app
  - `app/main.py` - app factory, logging, router registration (`/api/v1`)
  - `app/api/` - routers (one module per resource)
  - `app/core/` - `config.py` (Pydantic settings, `extra="ignore"`), `database.py` (engine, `Base`, `get_db`)
  - `app/models/` - SQLAlchemy models; **every model module must be imported in `app/models/__init__.py`** (Alembic autogenerate and test setup rely on it)
  - `app/schemas/` - Pydantic request/response schemas
  - `app/services/` - business logic
  - `alembic/versions/` - migrations, hand-numbered `0001_…`, `0002_…` (`alembic revision -m "..." --rev-id 0002`)
  - `scripts/` - Python package for maintenance/import scripts, run as `python -m scripts.<name>` from `backend/`
  - `tests/` - pytest; DB tests use a testcontainers PostgreSQL 17 (`test_db`, `db_client` fixtures)
- `frontend/` - React SPA (JavaScript/JSX)
  - `src/router.jsx` - routes; `src/components/layout/` - app shell (header with patient switcher, bottom nav)
  - `src/lib/api.js` - shared API client (same-origin `/api/v1`, adds `X-Requested-With` on writes)
  - `src/contexts/` - `PatientContext` (Papá / Mamá / Ambos, remembered per device; the provider sits in `AppLayout` and loads `GET /patients`; `usePatient()` gives `patient`, `patients`, `selectedPatients`, `patientIds`)
  - `src/lib/jobs.js` - `useJob(id)` polls `GET /jobs/{id}` until done/failed; `src/components/ui/JobProgress.jsx` shows it as a progress bar
  - `src/lib/whatsappImports.js` - chunked export upload (`uploadWhatsappExport`, retries each chunk) and the imports list; page `src/pages/ImportarWhatsappPage.jsx` (lazy route `/importar-whatsapp`, users whose `me.can_import` is true, linked from the header user menu; there is no Ajustes page yet). `api.js` sends a `Blob` body as raw `application/octet-stream`
  - `src/lib/pdf.js` - `fetchPdf(path)` (cookie fetch → `File`) behind `components/ui/PdfButton.jsx`
    (two taps: prepare, then «Abrir PDF» / share); `src/lib/summaries.js` - pre-visit summary and
    emergency card (`/emergencia`, `EmergenciaPage`, linked from the header user menu)
  - `src/lib/upload.js` - `uploadAttachment(file)` (downscales photos to 2560 px, multipart via `api.js`) and `getAttachmentUrl(id)`
  - `src/pages/` - one page per bottom-nav section (+ `BitacoraEntryPage` at `/bitacora/:entryId`; `MedicosPage` / `MedicoPage` at `/medicos[/:doctorId]`, linked from the header user menu; `src/components/doctors/` holds the doctor edit and merge sheets and the admins' «Completar médicos con IA» panel; `CalendarioSuscribirPage` at `/calendario/suscribir` (personal ICS link + instructions), linked from the user menu and the Calendario header)
  - `src/lib/bitacora.js` - the **one place** for bitácora enum labels (`entry_type`, `channel`), the filter <-> URL search-param mapping (`parseFilters` / `filtersToSearchParams` / `toApiParams`) and Spanish error messages; `src/lib/logEntries.js`, `src/lib/doctors.js` - TanStack Query hooks (writes invalidate the whole `['log-entries']` key)
  - `src/components/ui/` - `Sheet` (bottom sheet on phones, right drawer from `lg`) and `ConfirmDialog`, both built on `src/lib/useModal.js` (portal into `<body>`, siblings made `inert`, focus trap, Esc, focus restore); mount them only while open. Never use `window.confirm`
  - `src/components/meds/PrintButton.jsx` - «Imprimir / Descargar PDF»: fetches the PDF with the session (`fetchMedsPdf` in `lib/meds.js`), then «Abrir PDF» (blob URL) or «Compartir o imprimir» (Web Share with the file) on a second tap — a plain link would lose the cookie in an iOS home-screen app, and sharing needs a fresh tap
  - `src/components/bitacora/` - list cards, desktop table, filter bar, entry sheet (with the "Próxima cita" section, `NextAppointmentField`), doctor picker, attachments
  - Chat UI (`/chat`, `/chat/:sessionId`; `/chat/nueva` becomes `/chat/<id>` on the first message without remounting, so the stream continues): `src/lib/chat.js` (hooks `['chat','sessions']` / `['chat','session',id]`, `streamChatMessage` = `fetch` + `src/lib/sse.js`'s `createSseParser`, since EventSource cannot POST; `groupMessages` joins a turn's assistant rows into one answer). While streaming, the page shows stored messages up to the send, then the live answer (deltas rendered at most every 80 ms); when the stream ends, or once a re-read after a dropped connection shows `generating: false`, it renders the stored session only. `src/components/chat/` holds the composer (fixed above the bottom nav, or above the keyboard via `visualViewport`), the message blocks and `AttachmentChip` (files from the message view's `attachments`, opened with a fresh presigned URL on tap)
  - `src/pages/MedicamentosPage.jsx` (lazy) + `src/components/meds/` - the medication list (`MedListView`, printed-sheet order, EVITAR) and its editor (`MedListEditor`, lazy): a working copy in page state keyed by patient (switching patient keeps it; leaving the tab with unsaved changes asks via `useBlocker`), item / EVITAR sheets, and «Guardar» → `PUT … dry_run` → `SaveSheet` (editable summary + `MedDiff`) → real `PUT` with `expected_version`; a 409 is never merged or retried. «Ambos» stacks both lists, each with its own «Editar». `src/lib/meds.js` holds the hooks (`['meds', patientId]`), display helpers (`formatQuantity`, `formatDays`), the EVITAR name check (`itemWarnings`, shown in the item sheet; `SaveSheet` shows the dry run's `warnings`) and `useMedChecks` (`OutdatedBanner`). Historial: `MedHistorialPage` (`/medicamentos/historial`, lazy; versions + «De la bitácora» events merged by `buildTimeline`, grouped by month; «Ambos» stacks both timelines; bitácora events have «Confirmar» / «Editar» (`MedEventSheet`) / «Descartar» and a «Por revisar (N)» box with a pending-only toggle and «Confirmar todos»; hooks `useUpdateMedEvent` / `useDeleteMedEvent` / `useConfirmMedEvents` invalidate `['meds', id]`) and `MedVersionPage` (`/medicamentos/historial/:n?paciente=<id>`, lazy; `MedDiff past` vs the previous version, «Ver lista completa de esta versión» = `MedListView` over `versionToList(snapshot)`; version 1 shows the list)
  - Calendar (lazy `/calendario?vista=agenda|mes&mes=YYYY-MM&dia=YYYY-MM-DD&cita=<id>`): `src/lib/appointments.js` holds the hooks (writes invalidate `['appointments']` and `['log-entries']`) and the Panamá-clock date helpers — `panamaToday()`, `startsAtISO(date, time)` (explicit `-05:00`); dates/times are read off the `starts_at` string (`appointmentDate`/`appointmentTime` in `bitacora.js`), **never** through `new Date()` in the device's time zone. Month grid weeks start on Monday. `src/components/calendar/` has the agenda, month view, `AppointmentSheet` and the Bitácora's `NextAppointmentCard`. Patient colors come only from `patientColor(slug)` in `src/lib/patients.js` (always paired with the name / an aria-label). «Marcar como realizada» opens `EntrySheet` with `appointment=…`, which saves through `POST /appointments/{id}/complete`
  - Labs (`/labs`; lazy `/labs/:reportId`, `/labs/examenes/:analyteId?paciente=<id>` and the admin `/labs/revisar-examenes`; «Subir resultado» is the lazy `components/labs/UploadSheet.jsx` over `src/lib/labUpload.js`; analyte hooks in `src/lib/analytes.js`): `src/lib/labs.js` is the one place for lab labels (flag, status), the filter <-> URL mapping and value/reference formatting; `src/lib/labReports.js` the hooks (an edit invalidates `['lab-reports']` and every `['analyte-series']`); `src/components/labs/` the cards, badges (a flag is always icon + word + color), edit sheet and `TrendChart.jsx` (hand-rolled SVG, no chart library; one chart per unit, never mixed units on one axis). `openAttachment(id)` (new tab / download with a fresh URL) lives in `src/lib/upload.js`
- `brand/` - logo source (`n16chat-logo-source.jpg`, a reduced copy of the operator's original, no
  personal data) and `build_icons.py`, which traces it with potrace into the SVGs
  (`frontend/src/assets/brand/logo.svg` = landscape logo: sign-in, header from `sm`;
  `cloud.svg` = the cloud mark: header on phones, where the switcher leaves ~30 px;
  `frontend/public/favicon.svg` copies `cloud.svg`, `extension/src/shared/logo.svg` copies `logo.svg`) and renders every icon PNG
  (favicons, apple-touch, PWA + maskable, `extension/icons/`). Never hand-edit those outputs:
  rerun the script (throwaway venv, see its docstring). Logo teal `#106c69` = `brand-700`.
  Workbox `globPatterns` includes `svg`/`png` so the bundled logo is in the app-shell cache
- `scripts/run-tests.sh` - canonical backend test/lint runner (docker)
- `docker-compose.yml` - local dev stack
- `docs/` - design, deploy, import (`docs/import.md`) and sync-extension API (`docs/extension-api.md`) docs
- Demo data (#142, `docs/demo.md`): `python -m scripts.demo_seed` (records in
  `scripts/demo_seed_data.py`) resets and seeds synthetic data, and
  `python -m scripts.demo_export import` feeds the committed synthetic WhatsApp
  zip (`backend/scripts/demo_data/`, the one `*.zip` `.gitignore` allows)
  through the `daily` pipeline; AI keys via the opt-in `docker-compose.demo.yml`.
  Both refuse anything but the local compose stack (`check_local`: environment
  `development`, DB `db:5432/n16chat`, storage `minio`; no override). Keep every
  value invented; a new data table must be added to `demo_seed.DATA_TABLES`
- `extension/` - the WhatsApp Web sync Chrome extension (#45, MV3, vanilla JS, no build; README has
  load-unpacked + calibration steps): popup «Sincronizar», options (API base, `n16x_` key in
  `chrome.storage.local`, group name, dry run on by default). Flow = recon option A: «Exportar
  chat» (text) → `daily` run with `expects_media` → `media-missing` → per bubble: context menu
  «Descargar», captured in memory by the MAIN-world `capture-main.js` (blob anchor /
  `showSaveFilePicker`, never the disk; an unanswered capture quarantines the page until reload,
  and every sync reloads the WhatsApp tab first) → upload with `wa_key` → `media/complete`; each run also retries earlier runs' missing files
  of the last 14 days (`since`), newest first, with a local skip book of failed message ids
  (Opciones «Reintentar archivos omitidos» empties it). Locating (#118): a row's day is
  re-read on every scan (`match.assignDays`, DOM order + text rows' pre-plain dates; a
  conflict it cannot settle → no day, never a guess), senders spread over a run
  (`propagateSenders`, stopped by a conflicting label; quoted authors ignored); a `not_found` records its funnel stage
  (`match.explainMiss` → `nf_<stage>`, `notFound` in `n16Run`, popup «Diagnóstico»: ids,
  numbers and label shapes only). **Every
  selector and UI label lives in `src/content/selectors.js`**; any unexpected UI state stops with
  a Spanish status (its UI labels only from `LABELS.known`, others counted: unknown labels can
  carry names); Escape only via `dismissOverlay` (with nothing open it closes the chat); never
  touches the composer. Tests: `cd extension && npm test` (pure helpers,
  `node --test`; not in CI)

- `docker/backend/Dockerfile.prod`, `docker/frontend/Dockerfile.prod` (+ `nginx.prod.conf`) - production images (repo root as build context, `.dockerignore` at the root)
- `docker-compose.prod.yml` - production stack in `~/n16chat` on the droplet (services/containers `n16chat-backend`, `n16chat-worker`, `n16chat-frontend`); `.env.example` lists its settings
- `.github/workflows/deploy.yml` - builds and pushes the images to GHCR after green CI on `main`
- `backend/app/worker.py` - jobs worker entrypoint (`python -m app.worker`, service `worker` locally, `n16chat-worker` in prod); job handler modules are listed in its `HANDLER_MODULES` (today `app.services.whatsapp_import.jobs` (+ `whatsapp_media`), `app.services.labs.jobs`, `app.services.calendar_invites`, `app.services.transcription.jobs`, `app.services.media_classification.jobs`, `app.services.daily_analysis.jobs`, `app.services.daily_analysis.backfill`, `app.services.doctor_inference`, `app.services.ledd_substances`). `jobs.enqueue(..., delay=timedelta)` postpones a job's first run.
- `backend/app/core/logging.py` - logging setup, incl. redaction of URL-borne tokens from uvicorn's access log

## Coding Conventions

- Backend: ruff (`line-length = 100`, rules E/F/I/UP), formatted with `ruff format`.
  Sync SQLAlchemy 2.0 style. New settings get safe defaults so a missing env key
  never crashes startup, and are added to `.env.example`.
- Logs never carry secrets from URLs: keep token-bearing routes covered by
  `backend/app/core/logging.py`'s redaction (calendar `.ics` path, `…token=`
  query params).
- API: REST under `/api/v1`, JSON. `GET /api/v1/health` is unauthenticated and
  touches no database.
- Auth (`app/api/auth.py`, `app/services/auth.py`): protect every new endpoint
  with `Depends(require_user)` / `Depends(require_admin)` from
  `app/api/deps.py`. A global middleware rejects any non-GET/HEAD/OPTIONS
  request without `X-Requested-With` (CSRF) — tests must send it on writes.
  Magic-link tokens travel only in the URL fragment and are consumed only by
  `POST /auth/verify`; never add a GET that consumes one. Users are managed
  with `python -m scripts.users add|list|deactivate|import-access`.
- Import permission (#96, migration 0024): the WhatsApp import area
  (`/imports/whatsapp`, `/analysis-runs`, `/extension/token`, the
  «Importar WhatsApp» menu link and page) needs an admin **with**
  `users.can_import` (`User.is_import_admin`; `require_importer` by cookie,
  `require_import_admin` also by the extension's token, so a key whose owner
  loses it gets 403). Plain admins (doctor merge, analyte review) do not get
  it: new admins start with `false`; 0024 set `true` only for the admins of
  the time. `GET /auth/me` returns the effective `can_import`. Set/clear with
  `python -m scripts.users import-access <email> on|off` (`off` revokes the
  extension key); never key it on an email in code.
- Audit: every write endpoint records the change with
  `app.services.audit.record(db, user_id=…, action=…, entity=…, entity_id=…,
  before=audit.snapshot(obj), after=…)` in the same transaction. User-edited
  tables use `AuthorMixin` (`created_by`/`updated_by`). Patients' private
  fields are set in the app or with `python -m scripts.patients set`.
- Attachments: `POST /attachments` stores an *unlinked* row (type sniffed from
  the bytes, SHA-256 dedupe, key `<kind>/<yyyy>/<sha256>.<ext>`); the owning
  record links it on save with `app.services.attachments.link_attachments`.
  Files reach the browser only as presigned URLs from
  `GET /attachments/{id}/url` (≤ 10 min). Tests mock S3 with moto (`storage`
  fixture); `auth_client` is a signed-in client. Doctor name matching for
  importers: `app.services.doctors.match_doctors` / `normalize_name`.
- Doctor merge (`POST /doctors/{id}/merge`, admins, `merge_doctors`): locks
  both rows, repoints **every column with a FK to `doctors.id`** (found in
  `Base.metadata` by `doctor_references()`, so a new `doctor_id` /
  `prescriber_id` is covered once its model is in `app.models` — and
  `test_doctor_references_discovers_fks` must be updated to list it), merges
  aliases, fills empty contact fields, then deletes the duplicate. A new FK
  to doctors should be `ON DELETE SET NULL` or `RESTRICT`, never `CASCADE`.
- Bitácora (`app/api/log_entries.py`, `app/services/log_entries.py`): every new
  entry goes through `create_entry(db, data, user_id=…, origin=…, origin_ref=…,
  attachment_ids=…)` (importers pass `origin="import_docx"`,
  `review_status="pending_review"`); the API always writes `origin="manual"`.
  `PATCH` with `attachment_ids` replaces the linked set (removed files are
  unlinked via `unlink_attachments`, never deleted); `DELETE` is a hard delete
  whose audit row keeps the full entry. `POST /log-entries/review` (#58,
  admins, «Marcar todas como revisadas» on the list while «Por revisar» is
  on) takes `ids` or the list's `filter` (`LogEntryFilter`, read through the
  same `filter_conditions` as `GET`, so a new list filter must be added to
  both and to `toReviewFilter` in `src/lib/bitacora.js`); `mark_reviewed`
  locks the pending rows and sends each through `update_entry` (one audit
  row each; one invalid entry → 400, nothing changes). Full-text search uses the generated
  `search_vector` (`spanish` config over `public.immutable_unaccent(...)`,
  migration 0004): `unaccent()` itself is only STABLE and cannot go in a
  generated column or index, and any new query must normalize the same way
  (`search_condition`). The model also creates that function on
  `before_create`, so the test schema (`create_all`) works. PostgreSQL will
  not alter the type of a column a generated column depends on: changing
  `title`/`source_label`/`indications`/`family_notes` types (or the vector
  expression) means dropping `search_vector` (and its GIN index) and adding
  it back in the same migration.
- Labs screens API (`app/api/labs.py`, `app/services/labs/reports.py`):
  `GET /lab-reports` (filters `patient_id` (repeatable), `include_unassigned`
  for reports with no identified patient, `status`, `date_from`/`date_to` on
  the report date, `q` test name, `out_of_range`; `q` and `out_of_range` must
  hit the *same* result), `GET/PATCH /lab-reports/{id}` (any member; patient,
  dates, lab name, status `ok`/`needs_review`; audited as entity
  `lab_report`), `GET /analytes/{id}/series?patient=`. Numbers leave the API
  as JSON floats; `raw_extraction` is never returned.
  `DELETE /lab-reports/{id}` (#115, admins, `reports.delete_report`, UI
  «Eliminar informe» on `/labs/:reportId`) is a hard delete for a report of
  neither patient: results cascade, page files are unlinked (never deleted),
  the `lab_extractions` row stays (`report_id` NULL, outcome `lab_report`) so
  the file is never extracted again, a lab link stays `fetched`, and analytes
  the report used that are still `needs_review` with no result left are
  deleted in the same transaction (audited `delete` on `analyte`, `after`
  `{"orphaned_by_lab_report": id}`); reviewed analytes are never touched.
  The `lab_report` audit row is a summary (ids, dates, lab name, patient id,
  counts; no values).
- Chat proposals (#41, `tools.propose_change`, DESIGN §6.6): the chat's only
  way to change data — it adds a `pending` suggestion (`source="chat"`,
  `chat_session_id`, `created_by` = the asking member, migration 0021) after
  the schema, producer guards and `daily_analysis.pipeline.find_duplicate`;
  `update_entry` text is append-only (`add_indications` / `add_family_notes`
  through `pipeline.entry_changes`; full `indications` / `family_notes`
  refused: the chat sees only excerpts);
  every problem is an `is_error` result, never an exception. It commits at
  once (like a file tool), the engine sends an SSE `suggestion` event and
  stores the ids in the final answer's `meta.suggestion_ids`, which
  `GET /chat/sessions/{id}` returns as `suggestions` (current status) and
  the UI shows as `components/chat/ProposalCard.jsx` (link to `/pendientes`).
- Suggestions / «Pendientes» (`app/services/suggestions.py`, `app/api/suggestions.py`,
  `frontend/src/pages/PendientesPage.jsx`, `components/suggestions/`): AI
  producers (#39 daily analysis, #41 chat) only ever call
  `create_suggestion(...)` (payload validated with the target API's schema,
  `app/schemas/suggestions.PAYLOAD_SCHEMAS`); nothing auto-applies. `approve`
  applies through the normal services so validation + audit are identical;
  never re-link a WhatsApp message's attachment to a record — use
  `attachments.copy_attachments` (new row, same stored object). The entry,
  appointment and doctor sheets take `prefill` + `submit` for «Editar y aprobar».
- Bitácora DOCX importer (`app/services/bitacora_import/`, CLI
  `python -m scripts.import_bitacora`, operator procedure in `docs/import.md`):
  `docx_parse` (DOCX → markdown, regex split by year headers and date lines,
  patient header dropped), `extract` (one Claude call per entry, strict JSON
  schema, `EXTRACTION_MODEL`/`ANTHROPIC_API_KEY`), `load` (staging JSON →
  `create_entry`, idempotent on `origin_ref` `source_sha256`/`entry_index`/
  `text_sha256`). `origin_ref.next_appointment` becomes appointments via
  `python -m scripts.appointments backfill`; `origin_ref.medication_events`
  becomes `med_events` via `python -m scripts.med_events backfill`
  (`app/services/med_events_backfill.py`: kind mapped by `KIND_MAP`, keyed
  by unique `(source_entry_id, source_index)` (migration 0014), discarded
  events found in the audit log are never recreated, counts only).
  Tests build a synthetic `.docx` in `tmp_path` and fake the client; never
  call the real API, never commit a `.docx`.
- Medication-list importer (`app/services/meds_import/`, CLI
  `python -m scripts.import_meds`, `docs/import.md` "Medication lists"): same
  two-phase flow (extract on the laptop → `private/meds-<patient>.json` →
  `--from-staging` on the droplet). Section headers → slots are mapped **in
  code** (`docx_parse.HEADERS` / `classify_header`; a qualified slot word,
  «DESPUÉS DEL DESAYUNO», keeps the slot and its qualifier goes into the
  items' instructions); headers are sent marked
  `[S<n>]` and the slot comes from the section **number** the model returns
  (its header label is only cross-checked); everything before the first
  header (patient name, CIP) is dropped, and only a DESDE there is the list
  date; a preamble line that is not the title, the name / ID line or DESDE
  (by shape only, `preamble_kind`, never real names) aborts the run before
  any call (`REVISAR preamble_unrecognized line=N`, #81: a dropped
  medication is never silent); one Claude call per document. `review.review_list` runs at extraction
  and again at load: items without a slot (EVITAR / no section) always block,
  possible missing items, table sections, label mismatches, leftover
  `bad_*` extraction codes and past end dates block unless
  `--accept-warnings`; text over a column limit is refused, never truncated.
  The load creates version 1 via `save_list` and is refused when the patient
  already has a version. List-level `list_note` (regimen table)
  and `effective_date` ("DESDE") live on `med_list_versions` (migration 0013)
  and in the snapshot; `save_list` carries both forward unless passed
  (`KEEP` default, #135; see Medications).
- Appointments (`app/api/appointments.py`, `app/services/appointments.py`,
  DESIGN §5.3): `starts_at` timestamptz, returned on the Panamá clock
  (`to_local`; naive input = Panamá); all-day = 00:00 Panamá + `all_day`.
  Cancel is `PATCH status=cancelada`, `DELETE` is a hard delete. The
  bitácora's "Próxima cita" (`next_appointment` on entry create/edit) goes
  through `sync_next_appointment` inside `create_entry`/`update_entry`
  (one appointment per entry via unique `created_from_entry_id`; `null`
  cancels; `realizada` is never touched). Entry links are `ON DELETE SET
  NULL`. Backfill rules: `app/services/appointments_backfill.py`,
  `docs/import.md`.
  `POST /appointments/{id}/complete` ("Marcar como realizada") takes a
  `POST /log-entries` body and, in one transaction (row locked), creates
  the entry and sets the appointment `realizada` + `result_entry_id`; only
  a `programada` one (409 otherwise), and the entry date cannot be in the
  future (bitácora rule), so the UI offers it from the appointment's day on.
- WhatsApp export parser (`app/services/whatsapp/`, pure, no DB):
  `parse_export(zip)` → `ExportParse` with `WaMessage`s (`sent_at` in
  America/Panama, sender, text, media_filename/kind/member, `dedupe_hash`);
  `parse_chat_text(str)` for the `.txt` alone. It only decompresses the chat
  `.txt` (capped at `MAX_CHAT_BYTES`) and lists members, never extracts or
  reads media, ignores unsafe member names; stream a file with
  `ZipFile.open(message.media_member)`. Day/month order is decided over the
  whole file (impossible dates, then backwards steps); a tie (every date
  reads both ways, e.g. a short sync export of 10/8-10/9 or 10/9-10/10)
  goes, in order (`_choose_order`, #117), to the uploader's `date_order`
  hint (the extension's, kept in `stats.date_order_hint`), to the reading
  with no message more than `FLOOR_SLACK` (2 days) before the `floor` (the
  newest message stored by other imports, when within `FLOOR_RECENT` of
  the reference), to no message in the future and the end closest to the
  `reference` time (`imp.created_at`), to the **shorter span** (10/9-10/10
  is 9-10 Oct, not 10 Sep-10 Oct: both end on 10 Oct), else day/month. All
  inputs are passed in; the parser stays pure. `stats.date_order` records
  the order used. Only tied files are affected, so stored hashes of
  decisive files never change. A `daily` import whose new messages are
  mostly day/month-swapped twins of stored ones (same sender + minute,
  `process.swapped_twins`, ≥ `SWAP_MIN`) fails with a `PermanentJobError`
  before storing anything; an import stored in the wrong order is repaired
  with `python -m scripts.whatsapp_imports fix-date-order <id> --from dmy
  --to mdy [--dry-run]` (`whatsapp_import/date_order.py`, `docs/import.md`).
  Production's baseline (2026-10-09 `backfill`) is a **phone**
  export (Android dash style, 12 h, **minutes only**: every stored `sent_at`
  is `:00`); WhatsApp **Web** exports (bracketed 12 h, **with seconds**,
  `<image omitted>`, `<document omitted> name.pdf`, `[forwarded]`) hash
  differently until `dedupe_hash` truncates to the minute
  (`docs/whatsapp-web-recon.md` §4a, #45). Validate a
  real export only with `python -m scripts.wa_parse <zip> --stats` (counts
  only; a day/month tie uses now, or `--reference <iso>` like the import's time); its output and tests must never show message text, senders or file names.
  `dedupe_hash` = sent_at **truncated to the minute** + sender + normalized
  text + a **has-media flag** (never the file name or kind: an export
  without media and one with media must hash the same message equally) + an
  occurrence suffix for identical repeats (same minute); changing an input
  that differs for stored rows makes every such message import again as
  new. The minute truncation (#45) was safe because the stored baseline is a
  phone export with minutes only (production had 0 of 16,496 rows with
  seconds), so no stored hash changed; it lets a WhatsApp Web export
  (seconds) dedupe against it. `sent_at` itself keeps the export's seconds. A
  document descriptor (`Informe.pdf • 2 páginas` around an `<adjunto: …>` /
  `documento omitido` marker) is dropped from the text, and without media it
  supplies the file name/kind, so iOS documents hash the same both ways.
- WhatsApp import (`app/services/whatsapp_import/`, API
  `app/api/whatsapp_imports.py`, models `app/models/whatsapp.py`, import
  admins only, see "Import permission"):
  the zip is uploaded in chunks (`POST /imports/whatsapp` → `PUT
  …/{id}/chunks/{i}` raw bytes, any order, repeatable → `POST …/{id}/complete`),
  each stored at once as an S3 multipart part (no API memory/disk state between
  requests; parts ≥ 5 MB except the last, `WHATSAPP_CHUNK_MB`). The
  `whatsapp_import` job (`process_import`) streams the zip from Spaces to a
  temp file, stores non-system messages with `ON CONFLICT (dedupe_hash) DO
  NOTHING`, and streams each wanted media member (per-file cap
  `WHATSAPP_MEDIA_MAX_MB`) into an attachment owned by the message
  (`labs_only` = PDFs only); a stored message without a file gets it from a
  later with-media export (`media_import_id`). Stats are recomputed from the
  DB so a retry counts once. Lab extraction hooks in with `@after_import` and
  `pdf_messages(db, import_id)`. Tests: synthetic zips; moto's
  `S3_UPLOAD_PART_MIN_SIZE` and `upload.chunk_bytes` are patched down to 1 KB.
- WhatsApp Web sync extension API (#43, `docs/extension-api.md`, DESIGN
  §6.8): every `/imports/whatsapp` endpoint uses `require_import_admin`
  (`app/api/deps.py`): an import admin by cookie **or** by the extension's bearer
  token (`app/services/extension_tokens.py`, model `ExtensionToken`,
  migration 0022: `n16x_…`, SHA-256 only, one active per admin, managed by
  cookie-only `GET/POST/DELETE /extension/token`, UI
  `components/imports/ExtensionPanel.jsx`; a request with an `Authorization`
  header ignores cookies). Never accept the token anywhere else. CORS only
  for `EXTENSION_ORIGINS` (`install_extension_cors` in `app/main.py`, no
  credentials). Web exports have no media, so a run is a `daily` import with
  `expects_media` (`awaiting_media`), then `GET …/media-missing`, `POST
  …/{run}/messages/{id}/media` and `POST …/{run}/media/complete`
  (`app/services/whatsapp_import/media_upload.py`; `media-missing` groups
  *twins* by `sent_at` minute + sender + text + `media_kind` (WhatsApp Web
  shows minutes only; the extension counts bubbles of one kind); 409s carry
  `X-Error-Code` (`run_closed`, `run_not_ready`, `wa_key_conflict`); the
  upload's optional `wa_key` (the bubble's 32-hex `data-id`) is stored in
  `whatsapp_messages.wa_key`, unique when set, migration 0023, and a key
  already on another message is a 409); the `whatsapp_media` job
  runs the `@after_media` hooks (transcription, classification, labs —
  register a hook there only if it looks at `media_import_id` files) and the
  daily analysis waits while `awaiting_media`. For such a run the
  `@after_media` hooks are skipped at the end of `process_import` and run
  only in `finish_media`. Runs never completed are finalized
  (`media_upload.finalize`, idempotent) by the delayed `whatsapp_media`
  `deadline` job (`process.MEDIA_WAIT` = 90 min after the import), by the
  analysis past that bound, and by `finalize_older_runs` when the same admin
  starts or completes a newer run; uploads past the bound answer 409.
  WhatsApp Web's DOM shows minutes only (`docs/whatsapp-web-recon.md`):
  the sync uploads the chat export; the DOM only locates media bubbles
  (`data-id` = 32-hex message key).
- Lab extraction (`app/services/labs/`, models `app/models/lab.py`, DESIGN
  §5.5/§6.5): the `@after_import` hook in `labs/jobs.py` only **enqueues** one
  `lab_extract` job per new PDF of a `labs_only` import (payload
  `attachment_id`/`source_message_id`; `daily`/`backfill` media go through
  the media classifier first, see below);
  the single entry point is `process.process_attachment(db, storage, settings,
  attachment_id, source_message_id=…)` (or `enqueue_extraction`); the
  manual upload uses `process_upload` (below). It takes an advisory lock on the SHA-256 and checks
  `lab_extractions` (one row per SHA-256: `lab_report` / `not_lab_report` /
  `failed` + code, model, token usage): a recorded file is **never sent to
  Claude again** (to force a re-run, delete its `lab_extractions` row).
  Transient API errors raise `JobError` and record nothing; a missing key,
  401/403/404 raise `PermanentJobError` and record nothing; file problems
  (400/413, refusal, truncated/invalid output, over `LAB_PDF_MAX_MB`) are
  recorded `failed`. The PDF goes base64 in a `document` block (32 MB / 600
  pages per request), streamed, strict JSON schema, `EXTRACTION_MODEL`.
  Patient (review over wrong attribution): CIP (`normalize_id_number`)
  unless the report name has a word the patient's name lacks (insured
  relative's cédula) or is one word another patient's name shares; else name
  words **equal** to the full name's; else (#98, `identify_patient`, only
  when the ID does not decide: none printed/on file, or not cédula-shaped)
  a `name_variant` (an **in-order** subsequence of exactly one patient's
  words starting with the first given name, "SURNAME, GIVEN" reordered,
  initials for missing words) that also carries a surname the children
  never carry (`distinguishes`: own 2nd surname, or Mamá's printed «de
  <husband>»; `split_full_name` assumes cédula order) — «Juan Carlos
  Pérez» / «Ana María Pérez» never decide — then Claude (`patient_llm`,
  `CLASSIFICATION_MODEL`, only the printed name/ID + patients' names, only
  `high` confidence **and** `distinguishes` → `llm`, always left
  `needs_review` by `process.confident`; the exact `name` rule also needs
  `distinguishes`; a married «de X» counts only when X is the other
  patient's 1st surname, `resolve_couple`); else NULL + `needs_review`. `python -m
  scripts.lab_reports rematch [--dry-run]` re-runs it for unassigned
  reports from `raw_extraction` (audited, counts only). Tests: the autouse
  `patient_llm` fake in `conftest.py`. Manual uploads run these steps
  too, before the hint (`apply_patient_hint`). Analytes: `normalize_key` of name/aliases **per specimen**
  (`sangre`/`orina`/…, unknown → `otro`); unknown → new analyte
  `needs_review`. Seed list lives in migration 0007 (tests load it from
  there). `value_text` keeps the raw value; `values.read_value` gives every
  reading (a single `.`/`,` before three digits is ambiguous) and
  `resolve_value` uses the report's `decimal_convention` (NULL if the range
  only fits the other reading), else the range alone, else NULL + report
  `needs_review`. The lab's flag wins, else computed from a plain number and
  the range (none when > 30x off it: unit mismatch). Logs: ids, outcome
  codes and token counts only. Tests use a generated synthetic PDF and a
  fake client with `beta.messages.stream`.
- Manual lab upload (#15): `POST /lab-reports/extract` (any member;
  `attachment_ids` from `POST /attachments` in page order + optional
  `patient_id` hint) checks the files (`process.check_files`: one PDF, or
  ≤ `extract.MAX_IMAGES` JPEG/PNG/WebP photos; HEIC refused) and queues
  `process.enqueue_upload` (payload `attachment_ids`/`patient_id`), run by
  `process.process_upload`; `GET /lab-reports/extract/{job_id}` gives
  `outcome`/`report_id` once the job ends. Photos go as `image` blocks in
  page order; the dedupe key (`process.extraction_key`, stored in
  `lab_extractions.sha256`) is a single file's SHA-256 or the SHA-256 of
  `"lab-pages:" + ordered SHA-256s`. The hint (`matching.apply_patient_hint`)
  never overrides the document: a CIP/name match to the other patient →
  unassigned `needs_review`; no match → `manual` only when no comparable ID
  and the name is absent or a subset of the hinted patient's, and a
  hint-only (`manual`) report stays `needs_review`. Attachments owned by
  another record (not null/`lab_report`) are refused. Every page is
  linked to the report (`LabReportOut.attachments`). With `lab_link_id`
  («Subir PDF» from a lab link, see below) the report's source is `link`
  (`process_upload(source=…)`), and several PDFs are several requests,
  one report each; photos are always one report. Non-link uploads unchanged.
- Lab portal links (#16, `app/services/labs/links.py`, `fetch.py`, model
  `LabLink`): a second `@after_import` hook (`labs/jobs.record_lab_links`)
  records lab URLs in the import's messages (`links.record_links`: host in
  `LAB_LINK_HOSTS` globs, or path/query mentioning resultado/laboratorio/
  pdf/lab; deduped on `url_hash` of the normalized URL; patient guess only
  when the text names exactly one patient) and queues `lab_link_fetch`
  (payload `lab_link_id` only) for **https + allow-listed** hosts; default
  empty = record only. `python -m scripts.lab_links scan` covers older
  messages and queues untried links after the setting changes. The fetch is
  an SSRF surface: `fetch.fetch_pdf` re-checks https/allow-list/public IPs
  (every resolved address) on every hop (≤ 3 redirects), pins the
  connection to the validated IP (Host header + `sni_hostname`), no cookies/
  env proxies, timeouts, ≤ `LAB_PDF_MAX_MB`, PDF sniffed from bytes. A PDF →
  `store_upload` + `enqueue_extraction(..., lab_link_id=…)` (report source
  `link`); anything else stays `pending` with a `reason` code; 429/5xx/
  network retry, then `unreachable`. **URLs are PHI**: never in logs, job
  payloads/messages or `entity_id`. API: `GET /lab-links`, `POST
  /lab-links/{id}/ignore`, and `lab_link_id` on `POST /lab-reports/extract`
  («Subir PDF» marks the link `fetched`). A link may hold several PDFs
  (#124): the extract accepts a `pending` **or** `fetched` link (`ignored`
  → 409), one request + job per PDF, each its own report with source
  `link`; the link keeps its first `attachment_id` / `job_id` /
  `report_id`. The UploadSheet in link mode takes several PDFs (never
  merged; per-file «Leyendo…» / «Listo» / «Reintentar», then a summary
  and «Ir a Laboratorios») and hints to upload only a day's consolidated
  PDF (else results double on the charts). Tests: `httpx2.MockTransport`
  with streamed responses + a fake resolver; never the network.
- Analyte review (`app/services/labs/analytes.py`): `GET /analytes`
  (`needs_review` filter, result count, example results), `PATCH
  /analytes/{id}` and `POST /analytes/{id}/merge` (admins). Merge mirrors
  `merge_doctors`: locks both rows, re-points every FK to `analytes.id`
  (`analyte_references()`; update `test_analyte_references_discovers_fks`
  when adding one), merges aliases by `normalize_key`, deletes the source,
  audits `merge` + `delete`; different specimens need
  `allow_specimen_mismatch`. A rename keeps the old name as an alias.
  AI triage (#100, `app/services/labs/triage.py`, prompt
  `app/prompts/analyte_triage_v1`, `docs/import.md`): Claude
  (`EXTRACTION_MODEL`, batches of `BATCH_SIZE`, reviewed catalogue as a
  cached block; names/aliases/specimen/units/example values only) proposes
  `merge_into` / `keep_as_new` / `unsure` per analyte, stored in
  `analytes.triage` (JSONB `none_as_null`, migration 0027 — deliberately not
  a FK table, which `merge_analytes` would re-point). CLI
  `python -m scripts.analytes triage [--dry-run] [--apply] [--yes]`
  (estimate first); admin API `GET /analytes/triage/estimate`,
  `POST /analytes/triage` (job `analyte_triage` in `labs/jobs.py`,
  proposals only), `POST /analytes/{id}/triage/accept|reject`,
  `POST /analytes/triage/accept-all`. Only `--apply` auto-applies, and only
  `auto_applicable` cases (a merge needs `units_match` **true**: a source
  with no unit at all stores `null` and stays a proposal, #113); applying
  always goes through `merge_analytes` / `update_analyte`. An analyte with
  any proposal is never re-sent. Progress is reported only before each
  call, so `REQUEST_TIMEOUT_SECONDS` x (1 + `MAX_RETRIES`) must stay under
  `jobs.LOCK_TIMEOUT` (same for `daily_analysis/analyze.py`; tested).
  Tests: `tests/test_analyte_triage.py` (fake client).
- Background jobs (`app/services/jobs.py`, model `app/models/job.py`, DESIGN
  §5.8): enqueue with `jobs.enqueue(db, kind, payload)` inside the caller's
  transaction (flushed, not committed); the API polls `GET /jobs/{id}` (any
  signed-in user; payload not exposed). Handlers are `@jobs.handler("kind")`
  functions taking a `JobContext` (`ctx.db`, `ctx.payload`, `ctx.attempt`,
  `ctx.progress(pct, msg)`), registered by adding their module to
  `app.worker.HANDLER_MODULES`. Handlers must be idempotent (retries with
  backoff, and stuck-job recovery reruns a job whose `locked_at` is older than
  `LOCK_TIMEOUT`, 15 min, so report progress more often than that). Payloads
  carry ids only; messages are generic Spanish; failures store the exception
  **type** only, except `JobError` / `PermanentJobError` (no retry) whose
  message is shown, so never put document text in it. Tests register throwaway
  handlers and call `jobs.run_next(session_factory)` directly.
- ICS feed (`app/api/calendar.py`, `app/services/calendar_feed.py`, model
  `CalendarFeed`): `GET|HEAD /calendar/{token}.ics` takes no cookie; the
  token (stored only as SHA-256, one active feed per user via a partial
  unique index) is the credential, so keep the exact path shape — the
  access-log redaction and the edge vhost's `location ~ ^/api/v1/calendar/`
  depend on it — and never log it. `GET/POST/DELETE /calendar/feed` manage
  it (POST returns the URL once); audit rows never hold the token or hash.
  The feed never includes appointment `notes` (it lands in Google/Zoho).
- Email invites (`app/services/calendar_invites.py`, `GET/PATCH /me/settings`
  for `users.email_invites`): `appointments.create/update/delete_appointment`
  queue the debounced `appointment_invite` job through its `on_*` hooks;
  pass `notify=False` for bulk/historic writes (the backfill does). Tests fake
  `app.services.email.send_message` and patch `calendar_invites.get_settings`
  to production; never send real mail.
- Medications (`app/models/medication.py`, `app/api/meds.py`,
  `app/services/meds.py`, migration 0012): `GET/PUT /patients/{id}/meds`,
  `…/meds/versions[/{n}]` (detail adds `previous_version_no` /
  `next_version_no`) and `…/meds/events` (`med_events`, newest first by
  `(event_date, id)`, with the source entry's id + date; #82: keyset pages
  `{items, next_cursor}` (`limit` ≤ 500, default 100; pass `next_cursor` back
  as `cursor`; Historial «Cargar más» via `useInfiniteQuery`), and
  `?review_status=pending_review` returns **every** pending event unpaged
  for «Por revisar» (`usePendingMedEvents`); the chat's `get_med_history`
  reads all events); review of those events (any
  member, audited): `PATCH …/meds/events/{eid}` (edit and/or
  `review_status: ok`), `DELETE` (discard = hard delete),
  `POST …/meds/events/confirm {ids}` («Confirmar todos»; only this patient's
  pending ones). `PUT` is a full-list save through
  `save_list(db, patient_id, items=…, avoid=…, expected_version=…,
  user_id=…)` (locks the patient row; stale `expected_version` → 409;
  removed items get `end_date` = yesterday Panamá, never deleted; empty diff
  → no version; `dry_run` previews). List-level `effective_date` («Desde»)
  and `list_note` (#135): **omitted** in `MedListSave` = `save_list`'s `KEEP`
  (previous version's value; an approved `med_change` keeps them), explicit
  `null` clears; both are in the diff (`list_changed`) and summary, so a
  change to only them is a version; the previous side is read from the
  version's **columns** (`snapshot_with_list_fields`). The editor sets them in
  `SaveSheet` («Desde» default today, each change re-runs the dry run).
  `python -m scripts.meds_versions repair-effective-date --by <email>
  [--dry-run]` (`app/services/meds_repair.py`) refills «Desde» lost before
  #135 from the nearest earlier version (never overwrites; audited). Snapshot/diff/Spanish summary are the
  pure `app/services/meds_diff.py` — keep it DB-free and table-tested
  (`tests/test_meds_diff.py`). `days` is `smallint[]` ISO weekdays, null =
  daily. Rules in `docs/DESIGN.md` §5.4.
- Medication checks (`app/services/meds_checks.py`, DESIGN §6.4; warnings
  only, never refuse a save): `avoid_warnings(items, avoid)` (whole-word
  match after `normalize`, small documented `SYNONYMS` brand ↔ generic
  table) feeds `warnings` on every `PUT …/meds` response; the frontend
  mirror is `itemWarnings` in `src/lib/meds.js` — change both together and
  add cases to the shared table `backend/tests/avoid_match_cases.json`
  (pytest + `npm run check:meds` in `frontend/`, not in CI).
  `GET …/meds/checks` lists entries dated after the current version's
  «Desde» with medication events (`med_events` rows + not-yet-backfilled
  `origin_ref.medication_events`), minus `med_check_dismissals` (migration
  0015; `POST …/meds/checks/{entry_id}/dismiss`, idempotent, audited);
  `OutdatedBanner.jsx` shows them on Medicamentos.
- LEDD (#133, `app/services/ledd.py`, DESIGN §5.4/§6.4): pure and
  table-tested (`tests/test_ledd.py`), computed **on read** from snapshot
  items (current list, dry run, versions, print, chat), never stored.
  `CATALOGUE` cites a source per entry (Tomlinson 2010 base, Jost 2023 for
  opicapone/safinamide) — bump `TABLE_VERSION` on any change. As-needed /
  conditional doses never count; an unreadable dose is `not_calculated`,
  never guessed. Names outside the catalogue: `ledd_substances` (migration
  0029, `app/services/ledd_substances.py`): `PUT …/meds` (real save only)
  queues one `classify_substance` job per new `name_key` (only name +
  strength go to Claude, `CLASSIFICATION_MODEL`); a name with a row is never
  re-sent; a proposal never counts until an admin confirms it
  (`POST /ledd/substances/{id}/confirm|reject`, `require_admin`, audited
  `ledd_substance`; UI in `components/meds/LeddBreakdown.jsx`).
  `python -m scripts.ledd queue` covers older lists and rows left `queued`.
  Frontend: `src/lib/ledd.js`, `LeddSummary.jsx` (hover popover / sheet).
- Printable PDFs (`app/services/pdf/`): Jinja templates in `templates/`
  (`base.html` = shared Letter layout and running footer; `meds.html`
  extends it) + `render_pdf(template, context)`, which renders in a
  WeasyPrint **child process** (`render.py`, stdin → stdout) so the API
  workers never keep WeasyPrint in memory, refuses every URL fetch, and
  never logs the renderer's output. Autoescaping is on; markdown goes only
  through the `markdown` filter (markdown-it, tables, raw HTML and images
  off). Call it from a sync endpoint. `GET /patients/{id}/meds/print.pdf`
  (`?version=n`) is the medication list (`app/services/pdf/meds.py`); file
  names carry the slug, never a patient name. WeasyPrint needs Pango,
  HarfBuzz and DejaVu fonts from the OS: keep `docker/backend/Dockerfile.prod`,
  `backend/Dockerfile` and the CI backend job's apt step in sync. Tests read
  the PDFs with `pypdf`.
- Chat (`app/api/chat.py`, `app/services/chat/`, DESIGN §5.7, §6.6;
  migration 0016): shared `chat_sessions`; `chat_messages` hold one row per
  Messages API message with the **exact** content blocks sent/received
  (thinking, `tool_use`, `tool_result`), replayed as stored by
  `history.build_messages` — never edit stored content, the system prompt
  (`prompt.SYSTEM_PROMPT`, static: date and author go in each family
  message's context line) or the tool list casually: they are the cached
  prefix and what replayed thinking blocks are bound to (requests send
  `block_binding: drop_block`, so a change degrades instead of failing).
  `POST /chat/sessions/{id}/messages` takes the `generating_since` lock
  (409 if busy), stores the message, **closes its DB session** and streams
  SSE from a background thread (`engine.TurnRunner`) that opens a short
  session per step — never hold a connection across a Claude call. The
  lock token is the turn's own timestamp: clear it only compare-and-clear,
  and keep `engine.LOCK_STALE` above the worst-case turn (see the comment
  there) if you change the deadline, timeouts or retries. Tools
  live in `tools.TOOLS` (read-only, patient slug or id, compact JSON with
  ids + dates, `cap_result`); a new tool is a `Tool(...)` entry plus tests
  in `tests/test_chat_tools.py`. `CHAT_MODEL` / `CHAT_TITLE_MODEL` /
  `CHAT_MAX_TOOL_ITERATIONS`; no key → 503. Tests use the fake client in
  `tests/test_chat.py` (`use_client`), never the real API. Never log
  message text, tool inputs or results (usage counts only).
  Archive / delete (#99, `app/services/chat/sessions.py`, migration 0026):
  `archived_at` hides a session from `GET /chat/sessions` unless
  `archived=true`; `PATCH /chat/sessions/{id} {archived}` (any member) and
  `DELETE` (admin or creator, `can_delete` on every session out) lock the
  row and answer 409 while `engine.is_generating`. Delete deletes the row
  (messages cascade, `suggestions.chat_session_id` SET NULL), unlinks a
  generated file whose `storage_key` another attachment row shares, else
  deletes the row and removes the object after the commit
  (`purge_objects`, best effort); its audit row holds ids/counts, never the
  title or text. UI: `components/chat/SessionActions.jsx` («⋮» popover on
  desktop, bottom sheet + long press (`src/lib/useLongPress.js`) on phones),
  «Archivadas» = `/chat?archivadas=1`.
- Chat file tools (`generate_pdf`, `generate_xlsx`; validation, size caps
  and the openpyxl workbook in `app/services/chat/files.py`): with
  `propose_change` (a suggestion, below), the only tools that write. The PDF is `app/services/pdf/chat_document.py`'s
  `render_chat_document(ChatDocument(title, sections, patient_name))`
  (template `chat_document.html`, reusable by #34; patient display name
  only, never the CIP); the tool **rolls back its session before
  rendering** so no connection is held. The file is stored as an unlinked
  attachment (`kind` `documents`, `attachments.PDF` / `attachments.XLSX`,
  name `<slug>-<yyyy-mm-dd>.<ext>`), announced as an SSE `file` event, and
  `TurnRunner._persist` links the turn's files to its final assistant
  message (also an interrupted one) in the same transaction. Storage comes
  from `app.api.chat.storage_factory_dep` (tests override it). XLSX cells
  are never formulas (text cells, Excel quote prefix on `= + - @`).
- Ready-made PDFs (#34): `app/services/previsit.py` / `emergency_card.py` copy
  the data into plain dataclasses, the caller gives the connection back
  (`db.rollback()`), then `app/services/pdf/summaries.py` renders. Only the
  emergency card (and the meds print) read `full_name` / `id_number` /
  `birth_date`; the chat tools return counts + file name, never those fields.
- Voice-note transcription (#36, `app/services/transcription/`, model
  `app/models/transcription.py`, DESIGN §6.7 step 3): `provider.py` is the
  interface (`get_provider(settings)` → `transcribe(data, filename=, mime=,
  language=, prompt=, keywords=)` → `TranscriptResult`; `TranscriptionError`
  with a short `code` + `retryable`/`config`); `openai_provider.py` posts to
  `/v1/audio/transcriptions` with `httpx2` (no `openai` SDK; `gpt-transcribe`,
  `languages[]`, `keywords[]`). The `@after_import` hook in
  `transcription/jobs.py` (`daily`/`backfill` only) queues one
  `transcribe_audio` job (`{"attachment_id"}`) per new audio SHA-256;
  `audio_transcripts` (one row per SHA-256, `done`/`failed` + code) means a
  file is **never sent twice** — the job takes an advisory lock on the hash,
  copies a known text to every message with that file
  (`apply_transcript`), and records only file problems (too_large,
  unsupported, rejected), never transient/config ones. Hints
  (`transcription/hints.py`) are a fixed generic prompt + medication and
  active doctor names (≤ 400 chars); nothing else leaves. Ogg Opus is sent
  as `.ogg` (no ffmpeg). Tests use an httpx `MockTransport` and a fake
  provider — never the real API. UI: `components/media/AudioMessage.jsx`.
- Media classification (#37, `app/services/media_classification/`, model
  `app/models/media_classification.py`, DESIGN §6.7 step 3): the
  `@after_import` hook (`daily`/`backfill` only) queues one `classify_media`
  job (`{"attachment_id", "source_message_id"}`) per new image/PDF SHA-256;
  `media_classifications` (one row per SHA-256) means a file is classified
  once, copied to `whatsapp_messages.classification` (`apply_classification`).
  `classify.py` is one `beta.messages.create` call (`CLASSIFICATION_MODEL`,
  default `claude-haiku-5-5`, effort `low`, strict schema `{category,
  description, patient_hint}`). Stickers (WebP ≤ 512 KB whose header says
  square ≤ 512 px, `webp_dimensions`, no decoding) and content already in
  `lab_extractions` as `lab_report` need no call. A `lab_report` queues
  `lab_extract` via `enqueue_extraction` (PDF, or a single photo:
  `process_attachment` accepts one JPEG/PNG/WebP) unless `lab_extractions`
  has the content; so does every recorded classification failure except
  `too_large`/`unsupported` (the extractor then decides). **`labs.jobs.enqueue_lab_extractions` handles `labs_only`
  only** — keep that split so no file is read twice. The description is
  non-identifying by prompt and never logged. Tests fake both clients.
- Daily analysis (#39, `app/services/daily_analysis/`, DESIGN §6.7 step 4):
  the `@after_import` hook (`daily` only, with new messages) queues one
  `analyze_import` job (`{"import_id", "waits"}`) delayed 2 min; the job
  re-queues itself (3 min, at most `MAX_WAITS` = 10) while a new message's
  `transcribe_audio`/`classify_media` job is still queued/running
  (`pipeline.waiting_media`), then goes ahead with what it has. New messages
  (`import_id`) are grouped by Panamá day; days with nothing to read are
  skipped without a call; one `beta.messages.create` per day/chunk
  (`ANALYSIS_MODEL`, default opus, effort `medium`, cached system prompt +
  cached record context from `context.build_context`, strict schema). **Prompt
  and schema are versioned modules in `app/prompts/`** (today
  `daily_analysis_v2.SPEC`, a `PromptSpec`, reusing v1's schema): change them
  by adding a new `_vN`, never by editing a used version. Proposals → `pipeline.to_payload` (schema "" / 0 = none) →
  `find_duplicate` (records and pending suggestions; entries only with the
  same `entry_type`, stricter without a doctor — prefer a near-duplicate in
  Pendientes over a hidden event) → `create_suggestion`. `update_entry` from
  the analysis is **append-only** (`entry_changes`: the model sends only the
  text to add; the code appends it to the entry's full current text).
  Each day takes a per-import advisory xact lock (`lock_import`; a second
  copy re-queues itself) and progress is reported before every call.
  `stats.analysis` on the import holds counts, `days_done` (idempotent: a day
  is never analyzed twice), usage/cost and `prompt_version`. At most
  `ANALYSIS_MAX_DAYS_PER_JOB` days per job; the rest continue in another job.
  Logs: ids and counts only. Tests fake the client.
  Explicit requests (#134): a message whose text starts with the word «Bot»
  («Bot:», «Bot corrección …»; `pipeline.is_bot_request`, case/accent-
  insensitive, not «Bote»/«robot») is tagged `[PEDIDO EXPLÍCITO]` in the
  day's text (v2 prompts: always answer it, copy the wording, a replacement
  = `stop` + `add` pair since `MedChangePayload` has no `replace`, never
  switch patients); every suggestion citing one gets the rationale prefix
  `suggestions.EXPLICIT_PREFIX` («Pedido explícito: »; no column — the API's
  `explicit_request` and the pending list's explicit-first order read it
  back), Pendientes shows a badge. Dedupe/scope rules still apply; skipped
  and unanswered requests are counted (`explicit_*` stats) and logged.
- Backfill analysis (#40, `app/services/daily_analysis/backfill.py`, API
  `app/api/analysis_runs.py` (import admins), model `AnalysisRun` (migration 0020),
  prompt `app/prompts/backfill_analysis_v2.py` (v1 + the explicit requests;
  reuses the daily v1 schema and v1's `run_instructions`), UI
  `frontend/src/components/imports/BackfillPanel.jsx` + `src/lib/analysisRuns.js`,
  operator procedure in `docs/import.md`): **never automatic** — a `backfill`
  import only stores/transcribes/classifies; an admin starts a run (patient,
  date range, `cross_check`, `reanalyze`, `max_cost_usd`) after
  `POST /analysis-runs/estimate`. A run reads **every stored message of its
  range** (`pipeline.range_messages`), not the import's new ones. The
  pipeline takes a *target* (import or run: stats, advisory lock
  `lock_target`, `import_id` vs `analysis_run_id` on the suggestion) and
  `Options` backfill fields (`patient_id`, `cross_check`, `preamble` = the
  run's instructions above the context, `today`, `meds_since`); backfill
  rules live in `pipeline.out_of_scope` (other patient, past appointment,
  med change up to the list's «Desde», cross-check `update_entry`/
  `med_change`) and `find_duplicate(include_rejected=…, entry_window_days=…)`.
  `run_analysis(should_stop=…)` is the cost guard / admin stop, checked
  under the lock before each day. Days done by an earlier run with the same
  patient + cross-check are stored in `stats.days_already_done` at creation
  and skipped. The run's status changes from the job go through
  `_set_status` (never overwrites `stopped`). Undo = `reject-pending`.
  Daily analysis behaviour must stay unchanged (`Options` defaults).
- Doctor inference (#125, `app/services/doctor_inference.py`, prompt
  `app/prompts/doctor_inference_v1`, API `app/api/doctor_inference.py`
  (admins), UI `components/doctors/DoctorInferencePanel.jsx` on Médicos +
  `src/lib/doctorInference.js`, procedure in `docs/import.md`): one
  patient's entries with no doctor and an `ENTRY_TYPES` type (never
  `observacion_familiar`), optional date range, minus entries with any
  `source="ai"` suggestion (rejected ones too) or a pending one proposing a
  `doctor_id`; batches of `BATCH_SIZE` with nearby entries that have a
  doctor and non-cancelled appointments (±`WINDOW_DAYS`, ids/dates/types/
  titles/specialty, never notes or patient identity), active doctors as the
  cached block, `ANALYSIS_MODEL`. Only `high` + an active doctor id →
  `create_suggestion(kind="update_entry", changes={doctor_id},
  source="ai", rationale=reason)` (migration 0028 widened
  `ck_suggestions_source`). `create_suggestion` refuses any other shape for
  `source="ai"` or an entry that has a doctor; `approve` answers 409
  (`ENTRY_HAS_DOCTOR`) once it has one (`is_doctor_fill`): never
  overwrites. `POST /doctor-inference/estimate` → `POST /doctor-inference`
  (job `infer_doctors`, payload patient id + date range + user id, one at
  a time); same lock-timeout bound as triage (tested). Pendientes shows
  source «IA», the entry (date, type, title), the doctor with specialty
  (`ctx.doctorLabel`) and the reason. Tests: `tests/test_doctor_inference.py`.
- DB pool budget: `pool_size=2, max_overflow=2` per process (shared cluster,
  see `docs/deploy.md`). Changing it is an operator decision.
- Frontend: ESLint (`npm run lint`, errors fail CI). Tailwind v4 via
  `@tailwindcss/vite`, brand colors in `src/index.css` (`brand-*`). The service
  worker caches the app shell only, never `/api` responses.
- Frontend data/UI rules: markdown (indications, notes) is rendered only through
  `components/bitacora/Markdown.jsx` (react-markdown, **no** `rehype-raw`, so
  raw HTML shows as text; pass `gfm` for pipe tables, e.g. a medication list's
  `list_note`). Attachment URLs are presigned and short-lived:
  fetch them with `getAttachmentUrl` right before use, never store them.
  `@tanstack/react-table` is **v9** (`useTable` + `tableFeatures`, not v8's
  `useReactTable`; its docs ship in `node_modules/@tanstack/react-table/skills/`).
  The desktop table and the entry detail page are lazy-loaded chunks.
- All user-visible text in Spanish.

## Local Development

Host ports are chosen not to collide with the operator's other stacks
(panama-in-context uses 5173/8000/5433, atelier-loyalty-app 5433):

| Service | URL / port |
|---|---|
| frontend (Vite) | http://localhost:5180 (proxies `/api` to the backend) |
| backend (FastAPI) | http://localhost:8010 (`/api/v1/health`, `/docs`) |
| db (PostgreSQL 17) | `localhost:5434`, user/db `n16chat`, password `localdev` |
| minio (S3 stand-in for Spaces) | API `localhost:9010`, console http://localhost:9011, user `n16chat`, password `localdev-minio`, bucket `n16chat` (auto-created) |
| worker (jobs) | no port; `docker compose logs worker`, `docker compose restart worker` after editing job code (no hot reload) |

```bash
docker compose up --build        # migrations run automatically on backend start
docker compose down -v           # stop and drop the local database (and MinIO files)
```

MinIO uses `bitnamilegacy/minio` (frozen): the official `minio/minio` and
`minio/mc` images can no longer be pulled from Docker Hub. The backend reaches
it as `minio:9000` but signs file URLs for `localhost:9010`
(`DO_SPACES_PUBLIC_ENDPOINT`), so `<img src>` works from the browser.

Frontend without docker: `cd frontend && npm ci && npm run dev` (proxies to
`localhost:8010`; override with `API_PROXY_TARGET`).

**Signing in locally**: the allowlist starts empty. Add yourself, request a
link at http://localhost:5180/entrar, and copy it from the backend log (in
`ENVIRONMENT=development`/`test` the magic link is logged, never emailed):

```bash
docker compose exec backend python -m scripts.users add you@example.com --name "Tú" --admin
docker compose logs backend | grep "DEV magic link"
```

**Tests** always via `scripts/run-tests.sh` (bind-mounts this worktree into a
throwaway container, publishes no ports, so it works next to a running stack):

```bash
scripts/run-tests.sh                                  # full suite, quiet
scripts/run-tests.sh pytest tests/test_health.py -q   # one file
scripts/run-tests.sh ruff check app tests alembic
scripts/run-tests.sh --build                          # after requirements.txt changes
```

## Shipping

PRs land unattended: pre-PR QA (`personal:pre-pr-qa`), then GitHub auto-merge
once the required checks pass — **Secrets scan (gitleaks)**, **Backend (pytest +
ruff)**, **Frontend (build)**. The `personal:ship` skill then follows this
section. Merge only with `gh pr merge <N> --auto --merge --delete-branch`; never
`--admin`, never the REST merge endpoint.

**Before the PR** (from the worktree, mirroring CI):
- `docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect --source /repo --no-git --redact --no-banner`
- `scripts/run-tests.sh` (full suite, always)
- `scripts/run-tests.sh ruff check app tests alembic`
- `scripts/run-tests.sh ruff format --check app tests alembic`
- for `frontend/` changes: `npm ci && npm run lint && npm run build` in `frontend/`

An exit 2 "…is EMPTY inside the container" from `run-tests.sh` is an
environment problem, not a result. Also re-read the diff for PHI (see Privacy Rule).

**What merging does**: CI runs on `main`; when it is green, the **Deploy**
workflow (`.github/workflows/deploy.yml`, `workflow_run`) builds only the
images whose side changed (`backend/`, `docker/backend/` → backend;
`frontend/`, `docker/frontend/` → frontend) and pushes them to GHCR as
`ghcr.io/wanderindev/n16chat-backend` / `ghcr.io/wanderindev/n16chat-frontend`
(`latest` + git SHA), then prunes old versions (keeps 5). `pic-watchtower` on
the droplet polls every 5 minutes (`--interval 300` in `~/pic/docker-compose.prod.yml`), pulls `latest` and recreates
`n16chat-backend`, `n16chat-worker` and `n16chat-frontend` (its log warns
"Could not do a head request … 404" for these images and falls back to a
regular pull — harmless; `Failed=0` in its "Session done" lines is what
counts). Doc-only merges build nothing. The workflow never touches the
droplet; migrations are not run automatically. Details: `docs/deploy.md` "Deploy pipeline".

**Verify the deploy** (live since 2026-10-08): the Deploy run on the merge
commit succeeds; within ~10 minutes the droplet container runs the pushed
image. Compare **manifest** digests, never the container's `.Image` (that is
the local image ID, which never equals a registry digest): the digest of the
merge-SHA tag (`docker buildx imagetools inspect ghcr.io/wanderindev/n16chat-backend:<merge-sha>`,
or the Deploy run's push log) must equal
`ssh <user>@<droplet-ip> "docker image inspect -f '{{index .RepoDigests 0}}' ghcr.io/wanderindev/n16chat-backend:latest"`,
and `docker inspect -f '{{.Image}}' n16chat-backend` must equal that image's
`docker image inspect -f '{{.Id}}' ghcr.io/wanderindev/n16chat-backend:latest`
— check this **only for the images the Deploy run pushed** (it builds only
the side that changed: the backend image runs `n16chat-backend` and
`n16chat-worker`, the frontend image `n16chat-frontend`; a frontend-only merge
has no backend `<merge-sha>` tag); `curl -s https://app.example.com/api/v1/health`
→ `"healthy"`; and
any new `backend/alembic/versions/` migration is applied on the droplet, after
Watchtower has pulled the new image, with
`ssh <user>@<droplet-ip> 'cd ~/n16chat && docker compose -f docker-compose.prod.yml run --rm n16chat-backend alembic upgrade head'`
— upgrade only, never `downgrade` or `stamp` (a destructive migration still
needs the operator's approval first, below).

**Ask the operator first** (stop and report instead):
- a migration that drops or rewrites data (DROP TABLE/COLUMN, destructive
  UPDATE/DELETE, type narrowing);
- any production DB write;
- any droplet / DigitalOcean state change (`docker compose up/down` on the
  droplet, restarts, `.env` edits, Spaces, DNS, the shared `pic-frontend` edge);
- a PR that needs a new env key must **name it in the PR body** and the ship
  report, since `Settings` ignores unknown keys and the feature stays off until
  it is set.
