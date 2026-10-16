# Sync API for the WhatsApp Web extension

Contract between the n16chat backend and the Chrome MV3 extension (#45) that
syncs the family group from WhatsApp Web. Design and reasons: DESIGN §6.8.
Source of truth: `backend/app/api/whatsapp_imports.py`,
`backend/app/services/whatsapp_import/media_upload.py`,
`backend/app/api/extension.py`, `backend/app/api/deps.py`
(`require_import_admin`). Nothing here depends on WhatsApp Web's DOM: the
recon (#44) decides how the extension finds messages and files.

Base URL: `https://app.example.com/api/v1` (the app shows it next to the token).

## Authentication

- An **import admin** (admin with `can_import`, see `docs/import.md` "Who
  can import") opens «Importar WhatsApp» → «Extensión de Chrome» →
  «Generar clave» and pastes the key (`n16x_…`) and the API base into the
  extension's settings. The key is shown once; «Generar una nueva» revokes
  the old one, «Revocar» turns it off. Only its SHA-256 is stored.
- Every request: `Authorization: Bearer <key>`. Send no cookies
  (`credentials: "omit"`): a request with an `Authorization` header is judged
  by that header alone.
- Every non-GET request also needs `X-Requested-With: n16chat-extension`
  (any non-empty value; the app's CSRF rule applies to everyone).
- The key opens **only** the `/imports/whatsapp…` endpoints below, and only
  while its user is an active import admin: `401` (+ `WWW-Authenticate:
  Bearer`) for a missing/revoked/unknown key or a deactivated user, `403`
  when the user is no longer an admin or lost `can_import`. On `401` tell the operator to generate a new key.
- Never log the key, and never put it in a URL.

Why not reuse the app's session (as pic-extension reuses PIC's admin token)?
n16chat's session is an `HttpOnly`, `SameSite=Lax` cookie, which must stay
that way for browsers; a dedicated, revocable, narrowly scoped key is
simpler and safer.

### CORS

Service-worker fetches covered by `host_permissions: ["https://app.example.com/*"]`
bypass CORS. For fetches from extension pages without it, the backend answers
CORS for the origins in `EXTENSION_ORIGINS` (comma-separated
`chrome-extension://<32-letter id>`; empty default = no CORS), with methods
GET/POST/PUT/DELETE, headers `Authorization`, `Content-Type`,
`X-Requested-With`, and **never** `Access-Control-Allow-Credentials`. The id
of an unpacked extension is stable once its `manifest.json` has a `key`;
the operator sets the env key on the droplet (an operator step).

## A sync run

```
GET  /imports/whatsapp/last                          → where to start the export
POST /imports/whatsapp {mode: "daily", expects_media: true, filename, size, date_order?}
PUT  /imports/whatsapp/{run}/chunks/{i}  (raw bytes) → for every chunk
POST /imports/whatsapp/{run}/complete                → queues the import job
GET  /imports/whatsapp/{run}                         → poll until status done / failed
GET  /imports/whatsapp/media-missing?import_id={run} → messages without their file
POST /imports/whatsapp/{run}/messages/{id}/media     → one file (multipart "file")
POST /imports/whatsapp/{run}/media/complete          → no more files for this run
```

### 1. `GET /imports/whatsapp/last`

```json
{"last_message_at": "2026-10-08T21:14:00-05:00",
 "last_import_id": 41, "last_import_finished_at": "2026-10-09T02:20:11.5Z"}
```

`last_message_at` is the newest stored message (any import), on the Panamá
clock; `null` when nothing is stored. Export from that day on: overlapping
messages are deduplicated by the server (same `sent_at` **minute** + sender +
text + has-media, with an occurrence number for identical repeats in one
minute), so re-sending a day is harmless. The minute precision matters: the
stored baseline is a phone export (minutes only, everything up to
2026-10-08), a Web export carries seconds; if WhatsApp Web's export has no
date range, each run re-sends the whole chat and the server stores only what
is new.

### 2. Upload the text

The existing chunked upload (DESIGN §7), with `expects_media: true`:

- `POST /imports/whatsapp` `{"mode": "daily", "filename": "chat.zip", "size": <bytes>, "expects_media": true, "date_order": "mdy"}`
  → `201` with `id`, `chunk_size`, `chunk_count`, `awaiting_media: true`.
  `date_order` is optional (`dmy`, `mdy` or `ymd`; anything else is a 422):
  the order WhatsApp Web shows dates in (the extension's options setting,
  else a message's `data-pre-plain-text` date, else the page language). The
  import uses it only when the export's dates read both ways (a short
  export such as 10/9 - 10/10 is 9-10 October or 10 September - 10
  October); a date like 13/10 or 10/13 always decides. It is kept as
  `stats.date_order_hint`; `stats.date_order` is the order used (#117).
- A `daily` import whose new messages are mostly copies of stored ones with
  day and month swapped (same sender and minute) fails (`status: "failed"`,
  the job's error says the date order is ambiguous) and stores nothing; the
  extension stops the run as for any failed import.
- `PUT /imports/whatsapp/{id}/chunks/{i}` with `Content-Type:
  application/octet-stream` and exactly bytes `[i*chunk_size, (i+1)*chunk_size)`;
  any order, repeat on failure. `GET /imports/whatsapp/{id}` lists
  `received_chunks` to resume.
- `POST /imports/whatsapp/{id}/complete` → `status: "queued"`.
- Poll `GET /imports/whatsapp/{id}` until `status` is `done` (or `failed`;
  `stats.messages` has the counts).

The upload must be a **zip** containing the chat `.txt` (WhatsApp's name,
e.g. `WhatsApp Chat with <group>.txt`, or `_chat.txt`); a stored (no
compression) zip built in the extension is fine. If WhatsApp Web hands over
a plain `.txt`, the extension wraps it as `_chat.txt` (whether it does is a
calibration item, `extension/README.md`). The text must be WhatsApp's own
export format (the parser handles the Web form: `[6/17/25, 3:46:00 PM] Name:
…`, `<image omitted>`, `<document omitted> Name.pdf`, `[forwarded]`).

### 3. `GET /imports/whatsapp/media-missing`

Stored messages that have media but no file yet, oldest first.

| Query | Meaning |
|---|---|
| `import_id` | only the messages that import stored first (this run's new ones) |
| `since`, `until` | `sent_at` bounds (ISO; without offset = Panamá time). Without `import_id` and `since`: the last 30 days |
| `kind` | `audio`, `image`, `pdf`, `video` or `other` |
| `limit` | 1–1000, default 500 (`has_more` says there are more) |

```json
{"items": [
  {"id": 812, "import_id": 41, "sent_at": "2026-10-08T15:47:00-05:00",
   "sender": "<as exported>", "media_kind": "image", "media_filename": null,
   "caption": "<message text, ≤ 200 chars>", "forwarded": false,
   "twin_index": 0, "twin_count": 1, "wa_key": null}],
 "has_more": false}
```

`media_kind` comes from the export marker (`<image omitted>` → `image`,
`<voice message omitted>` → `audio`, `<document omitted> X.pdf` → `pdf`,
stickers → `image`, GIFs → `video`; an unknown marker → `other`).
`media_filename` is only known for documents.

**Matching** a WhatsApp Web message to an item: same sender, same
`sent_at` **minute** (Web exports carry seconds, the chat view shows minutes
only), same caption and same media kind. When `twin_count > 1`, several
stored media messages share sender, caption, `media_kind` and the `sent_at`
minute (even with different seconds): they are ordered by `sent_at, id` (chat order) and `twin_index` is
the position among them (counting twins that already have their file), so
the k-th such bubble in the chat is `twin_index = k`. `wa_key` is the
WhatsApp Web message key recorded by an earlier upload (see §4), so it is
`null` for a message still missing its file. If the extension cannot
tell them apart reliably, it should skip them rather than guess. Messages of
earlier runs that still lack their file are found with `since` (not
`import_id`), and are uploaded through the **current** run.

The response holds senders and captions (family PHI): keep it in memory,
never in logs or `chrome.storage` beyond the ids the extension tracks.

### 4. `POST /imports/whatsapp/{run}/messages/{message_id}/media`

`multipart/form-data` with a field `file` (let `fetch` set the boundary) and
an optional field `wa_key`: the matched bubble's `data-id` (32 uppercase hex,
`^[0-9A-F]{32}$`, else `422`). It is stored on the message
(`whatsapp_messages.wa_key`, unique when set): a key already recorded on
*another* message, or a different key on this one, answers `409` (a wrong
match), so a bubble is never attached to two messages.
`{run}` is the current run's import: it must be `done`, created with
`expects_media`, not completed or finalized yet, and its text import must
have finished less than **90 minutes** ago (`MEDIA_WAIT`). Upload a run's
files within that window, then complete it.

| Status | When |
|---|---|
| `201` | stored: `{"message_id", "attachment_id", "media_kind", "created": true}` |
| `200` | the message already has exactly these bytes (a retry): `created: false` |
| `400` | empty file |
| `404` | unknown run or message |
| `409` | run not `done`, not an extension run, already completed/finalized, or past its 90-minute window; the message has no media, or already has a *different* file; `wa_key` already on another message / a different `wa_key` on this one |
| `413` | over `min(WHATSAPP_MEDIA_MAX_MB, 199)` MB (JSON). A request body over 200 MB is refused by the edge nginx with its own `413` (HTML, not JSON) before reaching the API |
| `415` | not a known image/PDF/audio/video type (sniffed from the bytes) |
| `422` | the file's type does not match the message (`image` needs a photo, `audio` an audio, …; `other` accepts any known type); a malformed `wa_key` |

Some errors also carry a stable `X-Error-Code` header (exposed to CORS), so
the extension does not have to read the Spanish text: `run_closed` (the run
takes no more files: completed, finalized, past its window, or not an
extension run — upload the rest through the next run), `run_not_ready`
(its import is still being processed), `wa_key_conflict` (the bubble is
already another message's, or this message has another bubble; also when
two uploads race on the same key).

The file is stored once per content (SHA-256 dedupe), owned by the message;
its name is the exported document name, else the uploaded name. Uploads of
one run may be sent one by one, in any order.

### 5. `POST /imports/whatsapp/{run}/media/complete`

**Required**: call it at the end of every run, as soon as the run will not
upload more files (also when it uploaded none). Idempotent. Returns the
import (`media_completed_at` set). If files were uploaded, the
`whatsapp_media` job then queues their transcription / classification / lab
extraction, exactly as for files inside an export, and clears
`awaiting_media`. The run's daily analysis waits for this (and then for the
transcriptions).

The server recovers a run that never gets here (service worker killed,
browser closed), so its files are never lost — but only late:

- 90 minutes after the text import finished a deadline job finalizes it
  (the daily analysis does the same when it reaches that bound);
- creating (`expects_media: true`) or completing a newer run of the same
  admin finalizes that admin's older runs at once.

Finalized means: no more uploads to it (`409`), and its files go through the
pipeline as if «complete» had been called. Files the run did not get to stay
in `media-missing` and are uploaded through a later run. Until a run is
finalized its analysis waits, so a missed «complete» delays the day's
suggestions by up to 90 minutes.

## Errors and retries

Errors are JSON `{"detail": "<Spanish message>"}`, safe to show. `503` =
file storage unavailable: retry later. A run that dies halfway is safe to
repeat: the text dedupes, a re-uploaded file answers `200 created:false`,
and an unfinished run's import can be cancelled with
`DELETE /imports/whatsapp/{id}` while still uploading.

## Open points (calibration, #45)

The recon (`docs/whatsapp-web-recon.md`) answered how bubbles show time,
sender and keys; the extension (`extension/README.md`, «Calibración») still
has to confirm on the first dry run:

- what WhatsApp Web's export hands over (a `.txt` or a zip, through a blob
  anchor or the save picker), and whether its dialog has a date range;
- the download control's label and delivery for voice notes, documents and
  images, and the saved types (voice notes expected as Ogg Opus): types
  outside image/PDF/audio/video are refused with `415`;
- the Spanish UI labels and date format.
