# n16chat Sincronizar (Chrome extension)

A Chrome MV3 extension that keeps the family WhatsApp group synced into
n16chat from **WhatsApp Web**, with one click («Sincronizar»). It runs only
in the operator's own browser and only when clicked. Design: DESIGN §6.8;
API contract: `docs/extension-api.md`; WhatsApp Web DOM: `docs/whatsapp-web-recon.md`.

No build step, no dependencies: vanilla JS, ES modules in the service
worker, classic scripts in the page.

## What a run does

1. `GET /imports/whatsapp/last` (newest stored message).
2. Opens (or reuses) the `web.whatsapp.com` tab and selects the configured
   group: an exact title match in the chat list, else through the chat
   *search* box. Two matching chats → stop. The header title is checked
   before anything else happens.
3. Chat menu → «Exportar chat». WhatsApp Web's export is **text only**
   (every file `<… omitted>`), so there is no media option to choose. The
   file the page saves is captured in memory (below), wrapped in a zip if it
   is a bare `.txt` (member `_chat.txt`), and uploaded in chunks as a
   `daily` import with `expects_media: true`. The export dialog (calibrated
   2026-10-09) has a radio group «All messages» / «Custom date range»; the
   extension picks the custom range from the Panamá day of the newest
   stored message (`last_message_at`) to today, sets both native date
   inputs — found by their `aria-label` («Start date» / «End date», plus
   Spanish candidates), never by their order — and reads them back. With
   nothing stored yet it keeps «All messages». Any other dialog shape, or
   anything that fails while filling it, cancels the capture and the dialog
   and stops. The server stores only the messages it does not have
   (`dedupe_hash`, minute precision), so the overlap day is harmless.
4. Waits for the import to be `done`, then lists every message still
   without its file: this run's new ones (`media-missing?import_id=<run>`)
   plus earlier runs' (`media-missing?since=<now − «Reintentar … días»>`,
   default 14, at most 30), all pages. Earlier messages are uploaded through
   the current run (the API allows it). Ids in the local *skip book* (below)
   are left out. It works on at most «Archivos por sincronización» of them,
   and starts no new one after 80 minutes (the server closes a run's upload
   window 90 minutes after its import).
5. For each one: pages back to its day (`scrollTop = 0`; when that loads
   nothing, it scrolls the oldest row into view, then sends an upward wheel
   event, before concluding it reached the start of the chat — the count
   `paging_fallback` says how often a fallback was needed), finds the bubble by day (from the
   day separators in DOM order, checked against text rows' own dates and
   re-read on every scan; a disagreement it cannot settle leaves the rows
   with no day, i.e. skipped), minute (the last clock in the bubble's
   time), sender (its pre-plain name, else an author label outside a
   quoted message, spread over a run of bubbles from one person —
   including the operator's own runs once one of their text rows is
   loaded — but never across a row whose label conflicts), kind, caption (and file name for a
   document); when the server says there are *twins* (same minute, sender,
   caption **and media kind**) it picks the k-th only if exactly that many
   bubbles of that kind match, otherwise it skips. It also skips when any
   row of that day could not be read, and items whose kind is `other` (an
   unknown marker): a wrong match is worse than a skip.
   Then it opens the bubble's WhatsApp context menu, clicks «Descargar» /
   «Download», captures the file and uploads it with
   `POST …/{run}/messages/{id}/media`, sending the bubble's `data-id` as
   `wa_key` (the server refuses a bubble already matched to another message).
6. `POST …/{run}/media/complete`, always (also after a stop, so the files
   already uploaded go through transcription / classification at once).
7. Shows the counts and a link to «Pendientes» in n16chat.

The extension's key is valid only on `/imports/whatsapp…` (by design), so
it cannot read the Pendientes count: the popup links to
`https://app.example.com/pendientes` instead.

Run it a few times a day. **Files a run could not get are retried by later
runs** while they are younger than the retry window. Files are tried
**newest first** (the newest need the least paging back); once a day
cannot be loaded, every file of that day or older is booked `not_loaded`
without paging. A bubble
not found, ambiguous or unreadable, or whose download failed, goes into the
*skip book* (message ids and a reason only, in `chrome.storage.local`, at
most 1000) and is tried again after 2 days; a file the server refused
(wrong type, too large, conflict) and an `other`-kind item (never matched)
wait 30 days, i.e. they are not retried within the window. A failed
download is booked first and the run goes on with the next file, except
after a capture **timeout** (the page may still save that file later, so
the run stops and the page stays quarantined until the next sync reloads
it; that run skips the item), after a script error on one bubble (booked as
`script_error`, the run stops) or after 3 failed downloads
in a row (more likely a UI change: calibrate; a located row that is gone
at download time, `row_gone`, counts as one). Opciones →
«Reintentar archivos omitidos» empties the skip book, so a fixed version
retries everything at once. A key that is rejected
(401/403), a network or server error stops the run without booking
anything. `wa_key` is sent only for a 32-hex `data-id`. Files older than
the window are no longer tried; there is no screen in the app that lists
them. After a run the popup says how many files are still without upload
(and how many of those wait in the skip book). The server answers a
closed run with `X-Error-Code: run_closed`; the extension then stops
uploading and leaves the rest to the next run.

**Dry run** (Opciones → «Modo de prueba», **on by default**): steps 1–3
without the upload, then lists media still missing within the retry window
(read-only), locates and downloads those bubbles into memory, and reports
counts. Nothing is uploaded or created on the server, and the skip book is
not changed.

### Safety rules (in code)

- Every selector and UI label (English + Spanish candidates) lives in
  `src/content/selectors.js`; nothing is inlined in flow code.
- Any unexpected UI state (no menu item, an unknown dialog, two matching
  chats, a dialog with fields) stops the run with a Spanish status and the
  UI labels it saw, after «Cancelar» or Escape. It never clicks a control it
  did not identify by its exact label.
- Escape is pressed only while a menu or dialog is open (`dismissOverlay`):
  with nothing open, WhatsApp Web closes the chat on Escape. A visible
  dialog, or a visible menu item with a known label **outside the message
  list**, counts as open; a message row's hover buttons («React»,
  «Reply»…) never do. Before opening
  a bubble's context menu it closes any menu still open (whose «Descargar»
  would belong to another bubble), or stops with `stale_menu`.
- Stop reports (run state, popup) carry only labels from the known list
  `LABELS.known` in `selectors.js`, plus a count of the others
  (`unknown_labels`): a menu item like «Message <name>» or a button named
  after a file never reaches `chrome.storage`. Add a label there when a
  calibration report needs it.
- It never types into or clicks inside the message composer
  (`pointerClick` / `setInputValue` refuse it); the only text it enters is
  the group name in the chat *search* box. It never sends, forwards or
  deletes anything.
- Logs carry no message text, senders, file names or the key. Captions and
  senders from `media-missing` stay in memory; `chrome.storage.local` holds
  the settings, run counts / phases, the last uploaded `data-id` and the
  locate diagnostics (message ids, numbers and separator label *shapes*
  in which every word is `A`/`a` and every number `9`).

### How files are captured (no disk access)

`src/content/capture-main.js` runs in the page's **MAIN world** at
`document_start`. Only when the content script *arms* it (right before it
clicks «Exportar chat» or «Descargar»), it intercepts the first save the
page starts. The page **acknowledges** each arm, and the content script
clicks nothing that can save before that ack (`armed` in `dom.js`): window
messages arrive in a later task, and WhatsApp saves a voice note it
already holds in the same task as the «Download» click (seen live). No ack
within 3 s → a plain disarm (nothing was clicked, the page stays clean)
and the stop `capture_not_armed`; a quarantined page refuses the arm at
once → `capture_quarantine`. Save paths covered:

- an `<a download href="blob:…|data:…">` clicked through `.click()`,
  `dispatchEvent` or a real click — the Blob comes from the
  `URL.createObjectURL` call it recorded, else `fetch(blobUrl)`;
- `window.showSaveFilePicker` (File System Access API) — it returns a fake
  handle that collects what the page writes.

The save is cancelled (nothing reaches the download manager or the disk)
and the Blob is posted to the isolated content script, which hands it to
the service worker in 4 MB base64 slices. A download with an `https://`
URL is blocked and reported as `remote_url`.

**Late files.** Each capture has its own random id, and the content script
accepts only the answer for the capture armed now. A capture that is not
answered — it times out, it expires inside the page, the content script
abandons it (a new run starts while it is pending) or a new capture is
armed while it is still armed — puts the MAIN-world script into
*quarantine* **until the tab is reloaded** (no time limit): every save the
page makes is swallowed (neither delivered nor written to disk) and every
new capture is refused at once. The run then stops with «vuelve a
sincronizar». **Every sync starts by reloading the WhatsApp Web tab**,
which drops that page state, so a file clicked in one capture can only
be delivered under that capture's id, in the same page load. While a tab is
quarantined, a download you start yourself in WhatsApp Web is swallowed
too: reload the tab («recarga la pestaña») to download normally again.
(A save the page makes *twice* for one click would reach the disk the
second time, never another capture: only an armed capture receives files,
and arming while one is armed quarantines.)

`chrome.downloads` was **not** used: it only sees a download after the
browser starts writing it to disk, and reading the file back would need
file access. If calibration shows WhatsApp saves through a plain https
URL, revisit this (the status says `remote_url`).

## Install (load unpacked)

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
   select this `extension/` folder.
2. Open the extension's **Opciones** (right-click the icon → Opciones):
   - **Dirección de la API**: `https://app.example.com/api/v1` (only app.example.com
     over https or a localhost / 127.0.0.1 backend is accepted, so the key
     cannot be sent anywhere else);
   - **Clave**: in n16chat, «Importar WhatsApp» → «Extensión de Chrome» →
     «Generar clave» (admins), paste the `n16x_…` key (stored only in
     `chrome.storage.local`; leave the field empty later to keep it);
   - **Nombre del grupo**: exactly as WhatsApp shows it;
   - «Probar conexión» checks the key.
3. **EXTENSION_ORIGINS** (operator, on the droplet): the options page shows
   this extension's origin (`chrome-extension://<id>`). Put it in
   `~/n16chat/.env` as `EXTENSION_ORIGINS=chrome-extension://<id>` and
   recreate the backend container. The service worker's fetches to
   `app.example.com` are covered by `host_permissions` and normally bypass CORS,
   but the server only allows CORS for these origins (any other API base set
   in Opciones needs it). An unpacked extension's id is derived from the
   folder's path: load it from a fixed folder, or the id (and the setting)
   changes.
4. Open `https://web.whatsapp.com` and sign in. Every sync reloads the
   WhatsApp Web tab first (or opens one), so leave nothing half-typed there.

After editing the service worker or `manifest.json`, click the extension's
reload button; content-script changes also need a reload of the WhatsApp tab.

## Calibración (first run, by the operator)

The recon (#44) could not open the export dialog or a download menu, and
saw an English UI only. The first calibration (2026-10-09, English UI,
PRs #91–#94) confirmed the chat row click target
(`cell-frame-container`), the header «Menu» button, the export dialog's
date-range radio group and date inputs, and the arm ack; those entries are
`verified: true`. The date inputs' `aria-label`s («Start date» / «End
date») come from the calibration review and are `verified: false` until a
run confirms them (a mismatch stops with `export_dialog_fields`, `step:
custom`). Selectors still marked `verified: false` in `selectors.js` and
these flows must be confirmed with a **dry run** first (and again on a
Spanish UI):

1. Leave «Modo de prueba» on and click «Sincronizar (prueba)».
2. If it stops, the popup shows the Spanish status plus a short detail
   (`code`, UI labels seen, capture `reason`): adjust `selectors.js`
   (and `docs/whatsapp-web-recon.md`) and retry.
3. Things to confirm:
   - chat header «Menú» button label and the «Exportar chat» path (directly
     in the menu, or under «Más»);
   - the export dialog: its confirm button label, the date inputs'
     `aria-label`s (Spanish UI too), and whether the file arrives as `.txt`
     or `.zip`, through a blob anchor or the save picker (`exportVia` in
     the run state);
   - paging back: if `paging_fallback` shows up in the counts, `scrollTop =
     0` alone did not load older messages; note which runs needed it;
   - the bubble context menu («Descargar» / «Download») for voice notes,
     documents and images, and how the file is delivered; also that this
     context menu is rendered **outside** the message list (not inside
     `conversation-panel-messages` or a `[role="row"]`): the «Descargar»
     lookup ignores everything in the list, so a row's own hover
     «Download» button is never clicked in its place (#112). If the menu
     does render inside the list, every bubble skips with
     `download_item_missing`;
   - **locating** (#118): it is verified only after a dry run shows
     `located > 0` for a file of a day in the **past week** (not only
     today); until then it stays "to confirm". After «Reintentar archivos
     omitidos» (Opciones), run «Sincronizar (prueba)» and read
     «Diagnóstico» below;
   - day separator labels and date order in the Spanish UI (Opciones →
     «Orden de las fechas» overrides the guess);
   - own (outgoing) messages: their media bubbles have no author label;
     they are named only when one of the operator's own text rows of the
     same day is loaded (else `nf_sender`);
   - video / GIF bubbles (`videoThumb`) and non-voice audio (`audioFile`).
4. With a clean dry run, run one real sync on a quiet day and compare
   «Mensajes nuevos» with what was written that day: if messages already
   stored show up as new, stop and fix the normalization before syncing
   again (recon §4a "Caveats").

### «Diagnóstico» (#118)

When a run misses files, the popup shows a «Diagnóstico» line; open it for
the detail (JSON, counts, message ids and label shapes only — never text,
senders or file names). What to read:

- `nf_<stage>`: files not found, by the first step that left no bubble:
  `day_rows` (no row was put on that day: the day separators were not
  understood — see `sepShapes`, `seps_unparsed`, `sep_for_day`),
  `media_rows` / `kind` (no file bubble of that kind that day), `minute`
  (look at `minute_pm1`, `minute_tz`: one minute or whole hours off),
  `sender` (`sender_null` > 0: the bubble's sender could not be told),
  `caption`, `filename`.
- `notFound` (at most 25): per file, its server id, the stage and the
  numbers of its scan (`rows`, `rows_null_day`, `rows_on_day`, `seps`,
  `day_mode` `dom` / `layout`, `anchor_conflicts`, `other_day` = a full
  match the page put on another day).
- `nl_max_pages` / `nl_no_growth` (could not page back to the day: raise
  «Páginas del chat», or the chat stopped loading), `nl_skipped_older`
  (older files skipped after that) and «día más antiguo cargado»
  (`oldestLoadedAge`, in days).

Console probe used in the #118 diagnosis (read-only, shapes and counts
only): `docs/whatsapp-web-recon.md` §5 "Locating".

The device clock must be on Panamá time: «Hoy» / «Ayer» separators are read
against the device date, and stored times are Panamá wall clock.

## Development

```bash
cd extension
npm test        # node --test over tests/ (pure helpers only; nothing touches chrome.* or the DOM)
```

The CI does not run these tests (no required job was added); run them
before a PR that touches `extension/`.

Files:

| Path | Role |
|---|---|
| `manifest.json` | MV3: `storage`, `tabs`; hosts app.example.com, web.whatsapp.com, localhost dev ports |
| `src/background/index.js` | worker message router (popup / options) |
| `src/background/run.js` | the sync run |
| `src/background/api.js` | API client (bearer key, no cookies) |
| `src/background/state.js` | `chrome.storage.local` accessors |
| `src/content/selectors.js` | **the** selector and label registry |
| `src/content/match.js` | pure matching helpers (tested) |
| `src/content/dom.js` | DOM primitives, safe clicks, capture handshake |
| `src/content/main.js` | page commands: select chat, export, locate, download |
| `src/content/capture-main.js` | MAIN-world save capture |
| `src/shared/` | zip, export counts, run helpers (tested), CSS, `logo.svg` |
| `icons/` | toolbar icons 16/32/48/128 (generated by `brand/build_icons.py`, never edited by hand) |
| `src/popup/`, `src/options/` | UI (Spanish) |
