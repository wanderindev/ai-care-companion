# Bitácora import (one-time, from the Word document)

The family's bitácora `.docx` (one section per year, 2021–2026, ~390 dated
entries) is imported once into `log_entries` (DESIGN §6.2). The source file
and every intermediate file contain PHI: they stay outside git (`*.docx`,
`private/` and `exports/` are gitignored) and are deleted when the import is
done.

## How it works

`python -m scripts.import_bitacora` (code in `backend/app/services/bitacora_import/`):

1. **Parse**: DOCX → markdown with bold (`**…**`) and underline
   (`<u>…</u>`) kept; paragraphs are separated by a blank line;
   whitespace/NBSP-only paragraphs add nothing.
2. **Split** (regex, no AI): any line starting with `BITACORA` (any case,
   with or without emphasis or extra text, e.g. `BITACORA 2023 (cont.)`)
   opens a section and is never entry text; the patient header line(s) that
   follow (full name and ID number) are **dropped** and only counted. If a
   dropped line reappears verbatim inside an entry, the run **aborts before
   any API call** (PHI guard): remove it from the document and run again. Each date line alone on its line (`22 SEP
   2026`, `09 JUL 26`, `17 DIC 21`; months `ENE FEB MAR ABR MAY JUN JUL AGO
   SEP/SET OCT NOV DIC`) starts an entry, numbered in document order. The
   first line of each entry gives a *header hint* (doctor/source and a channel
   suffix such as `- WhatsApp` or `- correo`).
3. **Extract**: one Claude call per entry (`EXTRACTION_MODEL`, default
   `claude-opus-5-5`, effort `medium`, strict JSON schema, cached Spanish
   system prompt, 4 requests in flight, SDK retries with backoff on
   429/5xx). It returns doctor, source label, type, channel, title,
   indications (the doctor's words), family notes, next appointment and
   medication events. `indications` and `family_notes` are markdown (bold
   and paragraph breaks kept, underline removed); the other text fields are
   plain. The splitter's date always wins over the model's; an
   explicit channel suffix wins over the model's channel. Refused,
   truncated or failed calls are marked in the staging file, not retried.
4. **Stage**: everything goes to a JSON staging file (mode `600`, default
   `private/bitacora-staging-<hash>.json` inside a repo checkout; anywhere
   else, e.g. in a container, `--staging` is required). The file is checked
   and written **before the first API call** and rewritten every 10 entries,
   so an interrupted run keeps what it paid for: run the same command with
   the same `--staging` and it **resumes**, reusing the extractions already
   there (failed ones are retried). Review it before loading.
5. **Load**: each entry becomes a `log_entries` row with
   `review_status=pending_review` and `origin=import_docx`, committed one by
   one (a bad entry is reported and skipped, the rest load). Doctors are
   matched by name/alias (accents, case and titles ignored); unknown names
   are **created** (a bare title such as `Dra.` never creates a doctor; it
   stays in the source label) (listed at the end; merge duplicates in the app); a name
   matching several doctors leaves the entry without doctor and keeps the
   name in the source label. `origin_ref` keeps `original_markdown`,
   `next_appointment`, `medication_events`, the warnings and the idempotency
   key (`source_sha256`, `entry_index`, `text_sha256`).

Re-running is idempotent: entries already imported from the same file are
skipped (and not sent to Claude again). This also means **editing an
already-loaded entry in the staging file and loading again does nothing**:
correct loaded entries in the app. `--by <email>` is required whenever the
run loads, so the audit log records who imported. Exit status: `0` all good, `1` usage
or setup error, `2` finished but some entries failed (listed by number).

## Settings

- `ANTHROPIC_API_KEY` — required to extract (not to `--from-staging`).
- `EXTRACTION_MODEL` — optional, default `claude-opus-5-5`.

## Cost

Roughly **US$ 5–20 for the whole document** with `claude-opus-5-5`
(US$ 4 / 20 per million input/output tokens; output includes the model's
thinking, which dominates). Every run prints the tokens used and the
estimated cost; start with `--limit 10` and multiply the printed per-entry
cost by ~390.

## Procedure

The local dev image needs the new requirements (python-docx, anthropic)
once: `docker compose build backend` (and `scripts/run-tests.sh --build` for
the test image).

Run the extraction locally (the file never leaves your machine except to the
Claude API), review, then load into production from the staging file.

1. **Trial dry run** (local stack, file copied into the repo's gitignored
   `private/`):

   ```bash
   mkdir -p -m 700 private && cp ~/path/to/bitacora.docx private/
   docker compose run --rm --no-deps -v "$PWD/private:/data" -e ANTHROPIC_API_KEY \
     backend python -m scripts.import_bitacora /data/bitacora.docx --patient papa \
     --dry-run --limit 10 --staging /data/trial.json
   ```

   Check the printed **sections and entry counts per year** against the
   document (6 years, ~390 entries). Check `private/trial.json`: dates, doctors, `indications` vs
   `family_notes`, `next_appointment`, `medication_events`, `warnings`.
   Adjust and repeat if needed.

2. **Full dry run**: same command without `--limit`, `--staging
   /data/staging.json`. Review the staging file; fix extraction mistakes by
   editing its `extraction` objects directly (keep the JSON valid), or
   accept them and correct entries in the app later (they are all
   *Por revisar*). The load trusts the staging file's values but still
   validates them (dates, enums, header).

   **Before loading, check for leaked PHI**: confirm the per-year list and
   counts again, then search the staging file for the patient's surname(s)
   and ID number, e.g. `grep -n -i -e '<surname>' -e '<id number>' private/staging.json`
   (type them at the prompt rather than saving them anywhere). Expect no
   hits outside entries where the family wrote the name on purpose.

3. **Load into production** from the reviewed staging file (no Claude calls,
   no API key needed). Create a private temp dir on the droplet owned by the
   container user (uid 1000) and copy the file there:

   ```bash
   # on the droplet
   mkdir -m 700 /tmp/bitacora && sudo chown 1000:1000 /tmp/bitacora
   # from your machine
   scp private/staging.json n16chat-droplet:/tmp/bitacora/
   # on the droplet (file readable by uid 1000)
   sudo chown 1000:1000 /tmp/bitacora/staging.json && chmod 600 /tmp/bitacora/staging.json
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml run --rm -v /tmp/bitacora:/data n16chat-backend \
     python -m scripts.import_bitacora --patient papa --from-staging /data/staging.json \
     --by <your-email>
   ```

   Pass the `.docx` too (`/data/bitacora.docx --from-staging …`) to have
   the script check that the staging file came from that exact document.
   Note the doctors it created: merge duplicates in the app (admins:
   header menu → **Médicos** → the doctor to keep → **Combinar con…**).

4. **Appointments** (after migration 0009): turn the imported "Próxima
   cita" phrases into calendar appointments. Dry run first, then for real:

   ```bash
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
     python -m scripts.appointments backfill --dry-run --by <your-email>
   docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
     python -m scripts.appointments backfill --by <your-email>
   ```

   One appointment per imported entry whose `origin_ref.next_appointment`
   has a date (linked by `created_from_entry_id`; re-running skips those
   already done, so it can run again after a later import). The time, when
   given, is Panamá time; without one the appointment is all-day. The doctor
   is the one named (matched by name/alias), or the entry's own doctor when
   none is named; a name that matches no doctor (or several) leaves the
   appointment without doctor (the phrase as written is kept in its notes).
   A past appointment becomes **realizada**, with that entry as its
   `result_entry_id`, when a later entry of the same patient with the same
   doctor exists on that day or one day either side (closest first; each
   entry records at most one appointment). Past ones without such an entry
   stay **programada** (nobody recorded the visit; the calendar shows them
   as unconfirmed instead of guessing). Entries without a readable date, or
   with one before the entry's own date, are skipped and counted. So is a
   **repeated** "Próxima cita": when the patient already has the same visit
   that Panamá day (from an earlier entry, a previous run, the app, or one
   deleted in the app; a cancelled one counts too), the later entry gets no
   appointment of its own (`duplicate_same_day`; it stays unlinked, since an
   appointment links to one entry only). Same visit means: not two
   different times, and the same doctor. A side with no doctor at all (none
   named, none on the entry) fits any doctor, but a doctor **named** and not
   found in the directory only matches the same unknown name. An entry whose
   appointment was **deleted** in the app (the audit log says so) is skipped
   too (`deleted_before`), and that deleted visit stays occupied, so a re-run
   never brings back what someone removed, not even through another entry
   that repeats it; still, prefer *cancelling* a wrong appointment over
   deleting it. The unmatched-doctor count includes skipped repeats. It prints
   counts only. Exit status: `0` ok, `1` unknown `--by` user, `2` some rows
   failed validation.

5. **Medication history** (after migration 0014): turn the imported
   medication events into the Historial's «De la bitácora» events. Dry run
   first, then for real:

   ```bash
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
     python -m scripts.med_events backfill --dry-run --by <your-email>
   docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
     python -m scripts.med_events backfill --by <your-email>
   ```

   One `med_events` row per event in an imported entry's
   `origin_ref.medication_events`: the entry's patient and date (the
   extraction has no date per event), the kind mapped `start` → `inicio`,
   `stop` → `suspension`, `change` → `cambio_dosis`, `substitution` →
   `sustitucion`, the medication and detail as extracted, linked to the
   entry (`source_entry_id` + its position, `source_index`), **Por revisar**
   (`pending_review`). An unknown kind, or an event without a medication name
   (or one over 200 characters), is skipped and counted, never guessed or
   truncated. Re-running skips the events already created (whatever the
   family did to them since) and the ones **discarded** in the app (the audit
   log says so), so it can run again after a later import. The family
   reviews them in Medicamentos → Historial («Confirmar», «Editar»,
   «Descartar», «Confirmar todos»). It prints counts only; exit `1` for an
   unknown `--by` user. There is no cap on how many events a patient can
   have: the Historial loads them a page at a time («Cargar más»), and «Por
   revisar» / «Confirmar todos» always cover every pending event (#82).

6. **Clean up**: `rm -rf /tmp/bitacora` on the droplet and delete
   `private/*.json` (and the `.docx` copy) locally once the import is
   checked.

Extraction can also run directly on the droplet (the key in `~/n16chat/.env`,
`-v /tmp/bitacora:/data`, `--staging /data/staging.json`, and the `.docx`
copied there); without `--dry-run`/`--from-staging` the script extracts,
writes the staging file and loads in one go (`--by` required).

## Redo or undo

- **Retry failed entries**: run the full command again (or load an edited
  staging file); entries already imported are skipped, the rest are sent
  again.
- **Undo the whole import**: delete the rows it created. This is a
  production write: the operator runs it, after a backup/snapshot, in this
  order.

  If the appointments backfill already ran, delete its appointments **first**
  (deleting an entry keeps its appointments, only unlinking them):

  ```sql
  DELETE FROM appointments WHERE created_from_entry_id IN (
    SELECT id FROM log_entries
    WHERE origin = 'import_docx' AND origin_ref->>'source_sha256' = '<sha256>');
  ```

  If the medication-history backfill already ran, **first** delete its events
  too. This must run before the entries are deleted: deleting an entry sets
  `source_entry_id` to NULL, so afterwards this query matches nothing and the
  events stay behind, orphaned:

  ```sql
  DELETE FROM med_events WHERE source_entry_id IN (
    SELECT id FROM log_entries
    WHERE origin = 'import_docx' AND origin_ref->>'source_sha256' = '<sha256>');
  ```

  Then delete the entries:

  ```sql
  DELETE FROM log_entries
  WHERE origin = 'import_docx' AND origin_ref->>'source_sha256' = '<sha256 from the staging file>';
  ```

  (Doctors it created stay; deactivate or merge them in the app. Deleting in SQL
  bypasses the audit log, which is acceptable for an import never reviewed.)
  Then import again.
- An **edited or re-saved `.docx`** has a new hash, so it would import
  everything again as new rows: undo the previous import first. The load
  prints a warning listing entries whose text was already imported from
  another version of the document.

# Medication lists (one-time, from the two Word sheets)

Each parent's printed medication list (`.docx`, 1–2 pages) seeds **version 1**
of that patient's list (DESIGN §6.4). Same rules as the bitácora: the `.docx`
and the staging file contain PHI, live only under the gitignored `private/`
(and a temporary directory on the droplet), and are deleted afterwards.

## How it works

`python -m scripts.import_meds` (code in `backend/app/services/meds_import/`):

1. **Parse** (no AI): DOCX → markdown (bold/underline kept, tables as
   markdown tables). Section headers are recognized **in code** (accents,
   case, emphasis and a trailing colon ignored):

   | Header in the document | Slot |
   |---|---|
   | AL DESPERTAR, CUANDO DESPIERTE, AL LEVANTARSE | `al_despertar` |
   | DESAYUNO (CON/EN EL DESAYUNO) | `desayuno` |
   | MAÑANA (EN/POR LA MAÑANA) | `manana` |
   | ALMUERZO | `almuerzo` |
   | TARDE | `tarde` |
   | CENA | `cena` |
   | NOCHE, AL ACOSTARSE | `noche` |
   | MADRUGADA | `madrugada` |
   | RESCATE, SEGÚN NECESIDAD | `segun_necesidad` |
   | OTROS MEDICAMENTOS Y SUPLEMENTOS, OTROS MEDICAMENTOS, SUPLEMENTOS, OTROS | `otros` |
   | EVITAR | the EVITAR list (`med_avoid`) |
   | a clock time or window: `ENTRE 7:30 y 8:00 PM`, `8:00 PM`, `A LAS 3 PM` | by its start time: before 5:00 `madrugada`, before 12:00 `manana`, before 19:00 `tarde`, else `noche`; the item gets that `time` and the window is prefixed to its instructions |

   A slot word with a qualifier (`DESPUÉS DEL DESAYUNO`, `ANTES DE LA CENA`,
   `LUEGO DEL ALMUERZO`, `ANTES DE ACOSTARSE` → `noche`) is a header too:
   the word's slot, and the qualifier as written is prefixed to each item's
   instructions. With an amount (`30 MIN ANTES DEL ALMUERZO`) it is not: that
   reads as an item's instruction line.
   A header with a time in parentheses (`DESAYUNO (7:00 AM)`) keeps its slot
   and sets the time. **Everything before the first header** (title, patient
   name, CIP) is dropped and only counted; a `DESDE <fecha>` **there**
   becomes the version's `effective_date` (`30 SEP 26`, `30 de septiembre de
   2026`, `30/09/2026`). A DESDE line inside a section stays in the text: it
   is that medication's start date. Each header line is sent marked `[S1]`,
   `[S2]`… (its section number). A dropped line repeated later
   (a page-2 header) is dropped too. A remaining line that mentions `CIP` or
   `cédula` **aborts the run before any API call** (PHI guard): fix the
   document. So does a line **before the first header** that is not the
   title (`LISTA`/`MEDICAMENTOS`, no digits), **one** ID line (`CIP`,
   `cédula`, or letters plus one cédula-shaped number that is not a date),
   one letters-only name line above DESDE (none when the ID line already
   carries a name), or **one** DESDE line (it starts with `DESDE`, or
   `LISTA … DESDE`, and the rest is a readable date; a medication's own
   «… desde 1/10/26» is not one): it is printed as `REVISAR preamble_unrecognized
   line=N` (N = the document's N-th non-empty line, table rows included) —
   a medication with no header above it, or the items of a header the code
   does not recognize, would otherwise be dropped. `--accept-warnings` does
   not apply: put the lines under a recognized header (or rename the header)
   in your working copy, or remove them.
2. **Extract**: one Claude call for the whole document (`EXTRACTION_MODEL`,
   strict JSON schema). Per item: the **section number** it was under — the
   slot is that section's, from the table above (by position, not by the
   model's label); the header text it also reports is only cross-checked. An
   item from the EVITAR section or from no section gets **no slot** and
   blocks the load until you decide. Also: rescue flag (→
   `segun_necesidad`), name, strength, quantity as written (`½`,
   `¼`, `3 cápsulas`), dose note, time, weekdays (`lunes, miércoles y
   viernes` → `[1, 3, 5]`), `schedule_note` (taper date lists), instructions,
   prescriber, reason, start/end dates. EVITAR rows → `avoid`. A regimen
   table (one drug's schedule by hour) and general notes → `list_note`
   (markdown). Text is never cut: a value longer than its column (quantity
   50, strength 100, dose note 200, name 200, reason 300 characters;
   schedule note and instructions 10,000) is flagged `…_too_long` and the
   load refuses that item until you shorten it.
3. **Stage**: `private/meds-<patient>.json` (mode `600`; `--staging` is
   required outside a repo checkout). Edit only its `list` object
   (`effective_date`, `list_note`, `items` — each item's `slot` is final —
   and `avoid`). Running the extraction again with an existing staging file
   for the same document/patient/model **reuses** it (no new call); delete it
   to extract again.
4. **Review checks** (printed as `REVISAR`, and run again by the load on the
   edited staging file):
   - `item N: needs_review` — no slot (from EVITAR or no section).
     **Always blocks the load**: set its `slot`, or delete it from `items`
     and add it to `avoid`.
   - `possible_missing_items section=S lines=L items=I` — section S has more
     text lines (table rows not counted) than items taken from it (a line
     that wraps or a sub-heading also counts as a line);
     `possible_missing_avoid` — the same for EVITAR (its text lines plus
     its table data rows against the `avoid` entries, so a regimen table
     placed after EVITAR shows up here: check and accept);
     `table_section_manual_check section=S rows=N` — medication section S
     holds a table (N data rows, header row not counted) whose rows may be
     items or the regimen in `list_note`: check it by hand; `item N: unknown_header` / `header_mismatch` /
     `no_section` — the model's label disagrees with the position;
     `item N: bad_days` / `bad_time` / `bad_start_date` / `bad_end_date` /
     `bad_section` — the extraction could not read that value and left it
     empty (`bad_days` would load as **every day**): fix the field and
     **delete the code from the item's `warnings`**; `item N: ends_in_past`
     — the end date is before today, so the item would not be on the
     current list (check the year). These **block the load unless
     `--accept-warnings`** is passed, after checking them against the
     document (add the missing item to `items` with its `section` and
     `slot`, fix the field, or accept).
   - Item numbers are positions in the staging file's `items` (1-based),
     the same in `REVISAR` and in the load's validation messages.
5. **Load**: every item is validated with the API's schemas (one bad item →
   nothing saved, exit `2`, problems listed by item number and field), then
   `save_list` creates version 1 (summary "Lista inicial importada del
   documento Word.", `changed_by` = `--by`). A prescriber name matching
   exactly one doctor sets `prescriber_id`; otherwise it is kept in the
   instructions as `Indicado por: …`. **Refused (exit `1`) when the patient
   already has a list version**: the import only seeds version 1.

The output is counts, item numbers and warning codes only (never
medication names). Cost: one request per document, roughly **US$ 0.10–0.50**
with `claude-opus-5-5`; the run prints tokens and the estimate.

## Procedure

Needs migration **0013** applied in production first (see the deploy steps).

1. **Extract on your machine** (the only place with the API key), per
   patient. Copy the sheet into the gitignored `private/` under a short name:

   ```bash
   mkdir -p -m 700 private && cp ~/path/to/papa-list.docx private/meds-papa.docx
   docker compose run --rm --no-deps -v "$PWD/private:/data" -e ANTHROPIC_API_KEY \
     backend python -m scripts.import_meds /data/meds-papa.docx --patient papa \
     --dry-run --staging /data/meds-papa.json
   ```

   Same for `mama` (`/data/meds-mama.docx`, `--staging /data/meds-mama.json`).
   Check the printed **sections per slot**, the number of skipped preamble
   lines (title + name/CIP + DESDE: expect ~2–4; anything else there stops
   the run with `REVISAR preamble_unrecognized`, see above), `desde: found`, the items per slot against
   the sheet, the warnings and **every `REVISAR` line** (see "Review checks"
   above): compare each flagged section with the document.

2. **Review** `private/meds-papa.json`: resolve every `REVISAR` (items
   without a slot must be fixed; for the rest, fix or decide to pass
   `--accept-warnings`), then check slots, quantities (fractions), weekdays,
   `schedule_note` of tapers, `avoid`, `list_note`, `effective_date` (null
   when no DESDE was found in the heading: set it by hand). Search it for the patient's surname and ID number
   (`grep -n -i -e '<surname>' -e '<id number>' private/meds-papa.json`,
   typed at the prompt); expect no hits.

3. **Load into production** (no key needed). Optionally preview first with
   `--dry-run` (validates and runs the save, then rolls back):

   ```bash
   # on the droplet
   mkdir -m 700 /tmp/meds && sudo chown 1000:1000 /tmp/meds
   # from your machine
   scp private/meds-papa.json private/meds-mama.json n16chat-droplet:/tmp/meds/
   # on the droplet
   sudo chown 1000:1000 /tmp/meds/*.json && sudo chmod 600 /tmp/meds/*.json
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml run --rm -v /tmp/meds:/data n16chat-backend \
     python -m scripts.import_meds --patient papa --from-staging /data/meds-papa.json \
     --by <your-email> --dry-run
   docker compose -f docker-compose.prod.yml run --rm -v /tmp/meds:/data n16chat-backend \
     python -m scripts.import_meds --patient papa --from-staging /data/meds-papa.json \
     --by <your-email>
   ```

   Add `--accept-warnings` to both only if you checked the remaining
   `REVISAR` warnings against the document. Then the same two commands for
   `--patient mama` with `meds-mama.json`.
   Check the list in the app (**Medicamentos**).

4. **Clean up**: `rm -rf /tmp/meds` on the droplet; delete
   `private/meds-*.json` and `private/meds-*.docx` locally.

## Redo

Only while version 1 is still the only version (nobody edited the list in
the app). This is a production write: the operator runs it, after a backup:

```sql
BEGIN;
DELETE FROM med_items WHERE patient_id = (SELECT id FROM patients WHERE slug = 'papa');
DELETE FROM med_avoid WHERE patient_id = (SELECT id FROM patients WHERE slug = 'papa');
DELETE FROM med_list_versions WHERE patient_id = (SELECT id FROM patients WHERE slug = 'papa');
COMMIT;
```

(The audit log keeps the rows that were created.) Then load again. Once the
family has edited the list, correct it in the app instead.

## Repair «Desde» lost before #135

Before #135 every save from the editor (and every approved medication
suggestion) stored its version without «Desde» (`effective_date` NULL). Once
per environment, the operator refills each such version from the nearest
earlier version of the same patient that has one (column and snapshot; a
non-null value is never overwritten, so a second run changes nothing;
audited to `--by`; counts only):

```bash
cd ~/n16chat
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.meds_versions repair-effective-date --by <your-email> --dry-run
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.meds_versions repair-effective-date --by <your-email>
```

## LEDD: classify names outside the reference table (#133)

A list save queues one AI classification (`classify_substance`,
`CLASSIFICATION_MODEL`, only the medication name and strength are sent) for
each medication name the LEDD table does not know. Once after #133 is
deployed (migration 0029 applied), queue the names of the lists saved
before it; re-run it after setting `ANTHROPIC_API_KEY` if rows stayed
`queued`. A name with a row is never sent twice; counts only:

```bash
cd ~/n16chat
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.ledd queue --dry-run
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.ledd queue
```

Proposals never count toward the LEDD: an admin confirms each dopaminergic
(or uncertain) one from the «?» next to the LEDD on Medicamentos.

# WhatsApp backfill analysis (2026: Mamá's history, Papá's cross-check)

One-time (DESIGN §6.7 step 6, ticket #40): the family group's 2026 messages
become suggestions in «Pendientes» — for **Mamá**, entries/appointments/
doctors/medication hints that build her 2026 bitácora; for **Papá**, whose
bitácora is already complete, only events **missing** from it
(cross-check). Nothing is written to the records until someone approves a
suggestion. Everything runs in the app (import admins only, see "Who can
import" below): no CLI, no files on the droplet.

## Who can import

The whole import area — «Importar WhatsApp» (the menu link and the page),
every `/imports/whatsapp` endpoint, backfill analysis runs and the sync
extension's key — needs an **admin with `can_import`** (ticket #96). Being
an admin alone (merging doctors, reviewing analytes) is not enough.
Migration 0024 gave `can_import` to every admin that existed when it ran
(the operator), so nothing changed for them; an admin added later starts
without it.

Adding a sibling as an admin **without** import rights (on the droplet,
from `~/n16chat`):

```bash
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.users add <email> --name "<Nombre>" --admin
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.users list    # last column: "imports" or "-"
```

Nothing else is needed: the new admin sees no «Importar WhatsApp» link and
gets 403 from the import endpoints. To grant or take away the permission
later:

```bash
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.users import-access <email> on    # admins only
docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
  python -m scripts.users import-access <email> off   # also revokes their extension key
```

Even without `off`, an extension key stops working (403) as soon as its
owner is no longer an import admin.

## How it works

1. **Import** (Importar WhatsApp, mode **Histórico**): stores the export's
   messages (deduped against what is already stored) and its files; every
   audio is transcribed (OpenAI) and every photo/PDF classified (Claude
   Haiku, lab reports then go through the labs pipeline). A `backfill`
   import **never analyzes by itself**.
2. **Analysis run** («Análisis histórico», same page): patient, date range,
   «Solo lo que falta en la bitácora» (cross-check), «Volver a analizar días
   ya analizados», cost limit. It reads **every stored message of the
   range**, whichever import stored it — so it works even when the export's
   messages were already stored by an earlier `labs_only` or `daily` import
   (then the `backfill` upload only adds the missing files and their
   transcripts/descriptions). One Claude call (`ANALYSIS_MODEL`) per day
   with something to read; days with nothing to read (only emoji, stickers
   or photos classified as non-medical) are skipped without a call.
3. **Review** in «Pendientes» like any daily suggestion.

Rules the code enforces (on top of the prompt): only the chosen patient
(plus new doctors); no appointment before today (a past visit is an entry);
no medication change from a day up to the current list's «Desde» (the list
already reflects it — those changes belong in the entry's indications);
cross-check: never `update_entry` / `med_change`, and an entry with the
same doctor and type up to 2 days away counts as already recorded;
duplicates of existing records, pending **or rejected** suggestions are
skipped. Days already analyzed by an earlier run with the same patient and
cross-check setting are skipped unless «Volver a analizar» is ticked.

## Settings

- `ANTHROPIC_API_KEY`, `ANALYSIS_MODEL` (default `claude-opus-5-5`),
  `ANALYSIS_MAX_DAYS_PER_JOB` (14) — as for the daily analysis.
- `BACKFILL_MAX_COST_USD` — default cost limit per run (20).
- `BACKFILL_MAX_RANGE_DAYS` — longest range accepted (366).
- `OPENAI_API_KEY` (transcription) and `CLASSIFICATION_MODEL` for step 1.

## Cost

- **Analysis**: «Calcular costo» shows the estimate before anything runs
  (it measures the real messages and context; output assumed at ~2 000
  tokens per call, so it errs high). Expect roughly **US$ 0.03–0.06 per day
  with messages** with Opus: a full 2026 range (~280 days) ≈ **US$ 10–20**
  per patient; Papá's cross-check costs about the same as Mamá's run (same
  days read).
- **Import** (step 1): transcription US$ 0.0045 per audio minute;
  classification well under US$ 0.01 per photo/PDF; lab reports found cost
  an extraction each (`EXTRACTION_MODEL`). These run for **every** file of
  the uploaded export, whatever its date: export only the period you need
  (WhatsApp Web → Export chat → custom date range 2026-01-01 → today, with
  media) instead of the whole chat history.
- The run's live cost is on its card («x USD de y USD») and in the worker
  log; it stops by itself at the limit.

## Procedure

1. **Export** the group from WhatsApp Web: Export chat → custom range
   `2026-01-01` → today → **with media**. Keep the zip outside the repo
   (`exports/` is gitignored) and delete it when done.
2. **Upload** it at Importar WhatsApp with mode **Histórico**. Wait for the
   import to finish, then for its files: the worker log shows the
   `transcribe_audio` / `classify_media` jobs; «Calcular costo» (next step)
   says how many files are still in progress. Starting earlier is safe —
   the run waits up to ~2 h for them — but files still pending then are
   read as "not processed" for those days.
3. **Mamá**: «Análisis histórico» → Paciente **Mamá**, Desde `2026-01-01`,
   Hasta today, cross-check **off** → «Calcular costo». Check the days,
   messages and cost; adjust «Límite de costo» → «Iniciar análisis».
   Tip: a first run over one month (e.g. January) lets you judge the
   suggestions' quality and the real cost per day before the rest; a later
   run over the whole year skips the days already done.
4. Watch the card: status (En cola → Analizando → Terminado), «n de N días»,
   cost, suggestions created, link to the pending ones. On the droplet:

   ```bash
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml logs -f n16chat-worker | grep "backfill analysis"
   ```

   (ids and counts only; never message text).
5. **Papá**: same with Paciente **Papá** and «Solo lo que falta en la
   bitácora» **on**. Only one run per patient can be in progress; the two
   patients can run at the same time.
6. **Review** both in «Pendientes» (approve / edit and approve / reject).

## Stop, resume, undo

- **Stop**: «Detener» on the run's card. The job stops before the next day
  (a call in progress finishes); suggestions already created stay.
- **Cost limit reached**: the run shows «Detenido» and «Se alcanzó el límite
  de costo.». «Reanudar» asks for a new limit above what was already spent and
  continues with the missing days (days done are never repeated).
- **Failed** (missing/refused key, repeated API errors): fix the setting,
  then «Reanudar».
- **Undo**: stop it first if it is running, then «Rechazar sus pendientes»
  rejects every still-pending suggestion of that run (audited as
  rejections by you; approved ones are records now — correct those in the
  app). Rejected suggestions are never proposed again, even by a
  «Volver a analizar» run.

# Missing doctors of bitácora entries (AI, #125)

The analysis sets an entry's doctor only when a message names them, so a
rebuilt history has many consultations, exams, procedures and therapies with
no doctor. «Completar médicos con IA» (Médicos page, admins) asks Claude
(`ANALYSIS_MODEL`) to infer them from the entry text, the patient's nearby
entries with a doctor (±60 days), nearby appointments and the doctor
directory. Only **high-confidence** answers naming an **active** doctor
become suggestions in «Pendientes» (source «IA», the reason as «Por qué»);
nothing changes until someone approves, and approval is refused once the
entry has a doctor. An entry with a proposal from this feature (even a
rejected one) is never sent again. No new setting: it uses
`ANTHROPIC_API_KEY` / `ANALYSIS_MODEL`. Requires migration 0028.

## Procedure

Run it **after** the backfill analysis has rebuilt the history (above): the
nearby entries with a doctor are its main clue.

1. Finish and review the backfill runs for the patient (e.g. Mamá
   `2025-01-01` → `2025-12-31`, then `2026-01-01` → today), and approve or
   reject their pending suggestions, so new doctors are in the directory.
2. Médicos → «Completar médicos con IA» → Paciente (optionally Desde /
   Hasta) → «Calcular costo»: entries without a doctor, calls (~20 entries
   each) and the approximate cost.
3. «Iniciar» → confirm. The bar follows the job; at the end it says how many
   suggestions were created, with a link to «Pendientes». Worker log (ids,
   counts and tokens only):

   ```bash
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml logs -f n16chat-worker | grep "doctor inference"
   ```

4. Review in «Pendientes»: each card shows the entry (date, type, title),
   the proposed doctor with their specialty and the reason. «Aprobar» sets
   the doctor (audited as a normal edit), «Editar y aprobar» lets you pick
   another one, «Rechazar» discards it for good.
5. Run it again later (e.g. after adding doctors): only entries never
   proposed are sent.

# Lab reports without a patient (re-match, #98)

Lab reports whose patient was not identified at extraction (`patient_id`
NULL, «Por revisar») can be matched again from what was already extracted:
the name and ID number printed on the report (`raw_extraction`). The PDF is
never read or sent again.

## How it works

`app.services.labs.matching.identify_patient` (the same path every new
extraction uses):

1. **CIP**: the printed ID equals one patient's `id_number` → `cip` (unless
   the printed name has a word that patient's name lacks: an insured
   relative's cédula → stays unassigned, `id_conflict`). A cédula that
   matches nobody → stays unassigned (`id_mismatch`); an ID that is not
   cédula-shaped (a foreign lab's own number) is ignored and the name decides.
2. **Name**: the printed words equal the patient's `full_name` words (any
   order, accents, case) → `name`.
3. **Name variant**: the printed words (a «SURNAME, GIVEN» form put back in
   given-names-first order) appear in the same order in exactly one
   patient's name, starting with the first given name; only words are
   missing (second surname, married «de …» part; single letters are
   initials), **and** the name carries a surname the couple's children
   never carry: the patient's own 2nd surname, or for Mamá «DE <husband's
   surname>» with the «de» printed → `name_variant`. The children are
   «<given> <father's 1st surname> <mother's 1st surname>» and may carry
   their parents' given names, so «Juan Carlos Pérez», «Ana María Pérez» or
   «López, Ana» never decide (Claude, then review); «Ana Pérez López» fails
   the order too. Surnames are read from `full_name` in cédula order
   («<given…> <1st> <2nd> [de <husband>]»; `matching.split_full_name`), so
   `full_name` must be written that way.
4. **Claude** (`CLASSIFICATION_MODEL`, Haiku by default): sees only the
   printed name and ID and the two patients' full names; only a `high`
   confidence answer assigns → `llm`, and only when the printed name also
   carries a surname the children never carry (else `llm_not_distinct`). A
   name that is only the surname both parents share is never sent
   (`ambiguous`). The exact name (step 2) needs such a surname too.

A married «de X» counts only when X is the other patient's 1st surname (the
wife's «de <husband>»); otherwise («Pérez de León» as a 2nd surname) both
readings of the name are kept and a word that could be anyone's 1st surname
never distinguishes (`matching.resolve_couple`).

Assigned reports become `ok` only when nothing else keeps them «Por revisar»
(no results, or an ambiguous value → stays `needs_review`); a report Claude
assigned (`llm`) always stays «Por revisar» for a person to confirm. Each change is
audited (`lab_report`, `update`, no user). Reports a person assigned are
never touched.

## Procedure

1. Both patients' `full_name` (exactly as on the cédula, with the married
   «de …» part) and `id_number` must be set (`python -m scripts.patients set`,
   see `docs/deploy.md`). Done in production on 2026-10-09.
2. Preview (asks Claude for the reports that need it, saves nothing):

   ```bash
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
     python -m scripts.lab_reports rematch --dry-run
   ```

   It prints counts only: assigned per method (`cip`, `name`,
   `name_variant`, `llm`), how many would become «Revisado», still
   unassigned by reason (`id_conflict`, `id_mismatch`, `ambiguous`,
   `no_name`, `llm_no_match`, `llm_low_confidence`, `llm_error`,
   `llm_unavailable`, `no_names_on_file`) and skipped ones. The counts are
   approximate: Claude's answers may differ between the dry run and the
   real run.
3. Run it for real (same command without `--dry-run`). Running it again is
   harmless: it only looks at reports still without a patient (an
   `llm_error` is retried that way).
4. Assign the rest by hand in Labs («Por revisar»).

# Analytes under review (AI triage, #100)

«Revisar exámenes» lists every analyte the lab extraction created from a
test name it did not know (`needs_review`). `python -m scripts.analytes
triage` asks Claude (`EXTRACTION_MODEL`) to propose, for each one without a
proposal yet, «combinar con <examen revisado>», «confirmar como nuevo» (with
a cleaned name) or «no está segura», in batches of 30 with the reviewed
catalogue. Claude sees only test names, aliases, specimen, units and a few
example values as printed — never a patient, date or lab. An admin can also
start it from the page («Revisar con IA»); the page never applies anything
by itself.

## Procedure

1. Preview (asks Claude, saves nothing; prints the estimated cost first and
   asks to continue):

   ```bash
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
     python -m scripts.analytes triage --dry-run --apply
   ```

   It prints counts only: proposals per action, how many are high
   confidence, how many `--apply` would apply (`kept`, `merged`), skipped
   and errors (`truncated`, `invalid_json`, `refusal`, `missing`), and the
   token usage and cost.
2. Run it for real. Without `--apply` every proposal waits on the page;
   with `--apply` the high-confidence «confirmar como nuevo» and the
   high-confidence merges into a target of the same specimen whose units
   cover the analyte's are applied at once (an analyte with no unit at all
   has nothing to compare, so its merge always waits on the page, marked
   «sin unidades para comparar»), through the same merge /
   confirm services as the buttons (audited, no user). Completed batches
   are saved; running it again only sends analytes without a proposal.
3. Review the rest on «Revisar exámenes»: «Aceptar» / «Descartar» per card,
   or «Aceptar todas las propuestas». A refused one (different specimens, a
   name already used) stays pending with a note; handle it with
   «Confirmar» / «Combinar con…». A rejected proposal is never re-sent; to
   ask again for one analyte, clear its `analytes.triage` (SQL `NULL`).

# WhatsApp import read in the wrong date order (#117)

A short export whose every date reads both ways (10/9 - 10/10: 9-10
October, or 10 September - 10 October) used to be read as day/month when
the export was month/day. Its messages were stored a month early, mostly
as new copies of messages already stored on the right day. The parser now
breaks such a tie with the extension's `date_order`, the newest stored
message and the shorter span (CLAUDE.md "WhatsApp export parser"), and a
`daily` import that would store mostly day/month-swapped copies fails
before storing anything. `python -m scripts.whatsapp_imports fix-date-order`
repairs an import stored before that.

## How it works

For every message **first stored by the import** whose date changes when
day and month are swapped (a day above 12, or day = month, is left alone),
it recomputes `dedupe_hash` on the swapped date (same time, sender, text,
has-media flag and occurrence number):

- the hash is already stored → the message is a duplicate and is deleted;
- otherwise its `sent_at` and `dedupe_hash` are updated (re-dated).

Messages with a file, a transcript, a classification, or a lab report / lab
link pointing at them are **refused** and listed by id (decide by hand).
The original and swapped days leave the import's
`stats.analysis.days_done`, and `stats.date_order` records the new order,
so a second run is refused (it would swap the dates back). Every delete /
update is audited (`whatsapp_message`, `whatsapp_import`, no user). The
output is counts and message ids only.

## Procedure

1. Dry run (saves nothing):

   ```bash
   cd ~/n16chat
   docker compose -f docker-compose.prod.yml run --rm n16chat-backend \
     python -m scripts.whatsapp_imports fix-date-order <import_id> --from dmy --to mdy --dry-run
   ```

   Check the counts: duplicates to delete, messages to re-date, refused ids,
   skipped (hash not reproducible / conflicts), analysis days to clear.
2. Run it for real (same command without `--dry-run`).
3. Review or reject the import's pending suggestions in «Pendientes» (they
   were made from the misdated messages). The extension lists the
   re-dated messages' missing files under their right day on its next run.
