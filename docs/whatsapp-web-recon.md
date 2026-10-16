# WhatsApp Web recon (sync extension, #44)

Captured live on 2026-10-09 in the operator's Chrome (desktop, WhatsApp Web
**English UI**, `<html lang="en">`, device clock on Panamá time) with the
family group chat open. Feeds the selector module of the sync extension
(#45, DESIGN §6.8) and the API contract in `docs/extension-api.md` (#43).
Re-verify when the extension starts failing: WhatsApp Web ships often and
this file is the registry's documentation.

**Privacy**: this file records structure only. Every message text, name,
phone number, file name and message key is a placeholder (`<text>`,
`<name>`, `<key>`, `<file>`). The recon read the DOM with scripts that
returned tag names, roles, UI labels, attribute names, value *shapes* and
counts. It sent, downloaded, exported or clicked nothing in the chat.
Spanish labels are not verified (the UI was English). Where a Spanish label
is given it is marked *(unverified)*.

## 1. Page layout and chat selection

| Element | Selector | Notes |
|---|---|---|
| App root | `[data-testid="wa-web-main-screen"]` | |
| Left column | `#side`; chat list scroller `#pane-side` | |
| Chat search | `#side input[role="textbox"][aria-label="Search or start a new chat"]` | plain `<input type=text>`, same `placeholder`; inside `[data-testid="chat-list-search-container"]`. Spanish probably «Buscar un chat o iniciar uno nuevo» *(unverified)* |
| Chat list | `[role="grid"][aria-label="Chat list"]` | has `aria-rowcount`, so it is **virtualized**. Only about 18 `[role="row"]` (`data-testid="list-item-<n>"`) are rendered |
| Chat row title | `[data-testid="cell-frame-title"] [title]` | the `title` attribute holds the full chat name |
| Selected chat | `[aria-selected="true"]` inside the list row | |
| Filter chips | `All`, `Unread`, `Favorites`, `Groups` | buttons above the list |
| Open chat pane | `#main` | |
| Chat header | `[data-testid="conversation-header"]`; name in `[data-testid="conversation-info-header-chat-title"]` | header buttons: `aria-label` `Profile details`, `Group video call`, `Search`, `Menu` |
| Composer | `[data-testid="compose-box"]`, input `[data-testid="conversation-compose-box-input"]` (`role="textbox"`) | its `aria-label` is `Type a message to group <group>`. This is a cheap check that the **right chat** is open. The extension never types here |

To select the chat, focus the search input, insert the group name, wait for
a list row whose title `[title]` equals the configured name exactly, then
click it. Confirm with the header title or the composer's `aria-label`
before reading anything. If two rows match, stop: never guess.

**Export chat**: the operator confirmed that it lives in the chat `Menu`
(header) and has **no include-media option**. The Web export is text only,
with every file `<… omitted>`. The recon did not open the dialog; the
first calibration (2026-10-09, #45 follow-ups) found:

| Element | Selector | Notes |
|---|---|---|
| Chat row click target | `[data-testid="cell-frame-container"]` | the outer gridcell ignores synthetic clicks |
| Range: all | `input[type="radio"][name="date-range"][value="all"]` | «All messages» |
| Range: custom | `input[type="radio"][name="date-range"][value="custom"]` | «Custom date range»; shows two native `input[type="date"]` (max = today) |
| From / To | `input[type="date"]` with `aria-label` `Start date` / `End date` | taken by label, never by order (#102). Spanish labels *(unverified)* |

Still to confirm: the exact label path (`Menu` → `Export chat` or under
`More`), the confirm button label, and how the file arrives (`.txt` or
`.zip`, blob anchor vs save picker; `exportVia` in the run state).

## 2. Message list

| Element | Selector | Notes |
|---|---|---|
| Scroller | `[data-testid="conversation-panel-messages"]` | the element that scrolls (`scrollTop`, `scrollHeight`) |
| Message row | `[role="row"]` inside the scroller | one per message. Every row has a `[data-id]` |
| Message key | `div[data-id]` (`tabindex="-1"`) | **32 uppercase hex chars** (`^[0-9A-F]{32}$`). The old `false_<chat>@g.us_<key>_<participant>` form is **gone**: there is no chat or sender JID in any attribute. It equals the message key (`msg.id.id`) and stays the same across scrolls |
| Same key, second spelling | `[data-testid="conv-msg-<key>"]` | the same value as `data-id` |
| Rendered vs placeholder | `[data-virtualized="true"\|"false"]` inside the row | see §5 |
| Bubble | `[data-testid="msg-container"]` | |
| Sender label | `[data-testid="author"]` | contact name as saved on the operator's phone. It is **not on every bubble**: in a run of bubbles from one sender, later ones can lack it (seen on a text bubble) |
| Time | `[data-testid="msg-meta"]` | text `h:mm AM` (**minutes only**), for example `9:41 AM` |
| Text container | `div.copyable-text[data-pre-plain-text]` | **text messages only** (`type chat`), see §3 |
| Text body | `[data-testid="selectable-text"]` **not** inside `[data-testid="quoted-message"]` | a reply has two `selectable-text` nodes, and the first one is the **quoted** text |
| Reply | `[data-testid="quoted-message"]` (`role="button"`, `aria-label="Quoted message"`) | inside the `data-pre-plain-text` container |
| Day separators | leaf `<span>` with `Today`, `Yesterday`, a weekday or a date | **not inside any row**. The #118 probe (2026-10-10, English UI) saw them in normal flow (no sticky / absolute position) with distinct tops; the extension orders them with the rows by DOM order when they sit among the rows, else by layout, and lets a text row's `data-pre-plain-text` date anchor its stretch (§5) |
| Bubble tail | `[data-icon="tail-in"]` | marks the first bubble of a run |

Gone or unseen: the old `.message-in` / `.message-out` classes no longer
exist, and every other class name is obfuscated. Don't use any of them. No
loaded message was outgoing, forwarded, edited or deleted, so their markers
are **not verified**. Calibrate them in #45 (likely `Forwarded` and `Edited`
labels, and a "This message was deleted" stub).

### Message kinds (DOM)

| Kind | How to tell | Media in the DOM |
|---|---|---|
| Text | has `[data-pre-plain-text]` | none |
| Voice note | `[data-icon="ptt-status"]` / `[data-testid="ptt-status"]`; `button[aria-label="Play voice message"]`; `span[aria-label="Voice message"]`; a `role="slider"` with `aria-valuetext` `m:ss/m:ss` and `aria-valuemax` = seconds; a playback-speed button | **no** `<audio>` and **no** blob until it is played. The waveform is a `<canvas>`. The model's media stage is `RESOLVED` (keys known), but the file is not downloaded or decrypted |
| Image | `[data-testid="image-thumb"]` (+ `media-url-provider`); `div[role="button"][aria-label="Open picture"]` | once rendered: an `<img src="blob:https://web.whatsapp.com/<uuid>">` at full display resolution (seen 738×1600), next to a tiny `data:` placeholder. The blob is same-origin and readable by a content script with `fetch(blobUrl)`. It is **not proven** to be the original bytes (it may be a re-encode or downscale) |
| Document | `[data-testid="document-thumb"]`, type icon `[data-testid="document-PDF-icon"]` / `[data-icon="document-PDF-icon"]`; `div[title='View "<file>"']`; spans with `<n> page(s)`, `PDF`, `<size> kB` | **nothing** to read: media stage `INIT` (not downloaded). Getting the bytes needs the download action |
| Any media | `div[role="button"][aria-label="Forward media"]` | the extension never clicks it |

Download controls: the operator saw that right-clicking an audio or file
message offers download, and a text message offers only copy. The recon did
not open these menus or check how a download is delivered (blob anchor with
`download` attribute vs a browser download). Calibrate this in #45.

## 3. Sender, time and text per message

`data-pre-plain-text` (text messages only) has the shape:

```
[h:mm AM, M/D/YYYY] <name>: 
```

For example `[9:41 AM, 10/8/2026] <name>: `, with a trailing space. It was
checked on 13 loaded text messages against the message's internal epoch. All
13 matched **month/day** order in the **device time zone** (Panamá). None
matched day/month. The format follows the UI locale and the device clock, so
in a Spanish UI expect a different order or a 24 h clock, and parse
defensively.

- **Seconds: no.** The DOM shows minutes only (`data-pre-plain-text`,
  `msg-meta`). The internal message model has a Unix `t` **with
  seconds** (78 of 80 loaded messages had `t % 60 ≠ 0`), and that is where
  the Web export's `[…, 3:45:12 PM]` seconds come from. The production
  baseline is a **phone** export with minutes only (§4a). So DOM time,
  stored time and the phone export agree at the minute; only a Web export
  adds seconds.
- **Sender**: the saved contact name, the same string the export uses
  (`pre-plain` sender = `author` text on every checked row; no phone numbers
  or `~ name` forms among rendered rows). An unsaved number would show as a
  phone or `~ <pushname>` (not seen). Own messages are not verified (none
  loaded): the export writes the operator's name, and the DOM's pre-plain
  probably does too.
- **Text**: `selectable-text` `innerText` equals the stored body for plain
  messages, including multi-line (1 seen) and a 300+ character message. The
  quoted part of a reply must be skipped (see the table). Links are `<a>`.
  Emoji may be `<img alt>` (none seen). Long messages may be cut with a
  "Read more" link (not seen). Media rows have **no** pre-plain, so their date
  comes only from the day separators and the text rows around them (§5
  "Locating").

So from the DOM alone, an extension can rebuild `sender + minute + text` for
text messages reasonably well. It **cannot** rebuild seconds, but it does not need them against the minutes-only baseline once §4a lands. For media rows
it has to infer sender (from the run's first bubble) and day (from the
separators). For forwarded, edited and deleted messages the markers are
unverified.

### The internal model (MAIN world only, not recommended)

Every row's React fiber (`__reactFiber$<rand>` on `div[data-id]`, then
up the `.return` chain to the `msg` prop) carries WhatsApp's message model:
`t` (seconds), `type` (`chat`, `ptt`, `image`, `document`, …), `id`
(`fromMe`, `remote`, `id` = the `data-id`, `participant`), `body`,
`caption`, `filename`, `mimetype`, `size`, `duration`, `pageCount`,
`quotedStanzaID`, `isForwarded`, `latestEditMsgKey`, `mediaData.mediaStage`.
The chat model has `msgs` and `noEarlierMsgs`. These are private,
minified-bundle internals (`__x_` fields, `webpackChunkwhatsapp_web_client`).
They are reachable only from a `world: "MAIN"` script, because the isolated
content-script world cannot see expando properties. They can change on any
deploy, and reading them is the kind of client automation WhatsApp's terms
forbid. Use them for diagnosis at most, never as the data path.

## 4. Identifying a message stably

- **Within WhatsApp Web**: `data-id` (32 hex). It is stable across scrolls and
  reloads (it is the message key), so it is the right cursor for "last
  processed" and for retries.
- **Against stored messages**: the server has no `data-id` today. Matching
  uses `sent_at` + sender + caption at the **minute**, as
  `docs/extension-api.md` ("Matching") already says. Rows from the phone
  baseline have `:00` seconds, so their exact `sent_at` already is the
  minute. Rows stored from a **Web** export (every sync from #45 on) keep
  that export's seconds. Bursts are common (34 of the 80 loaded messages
  were voice notes, usually several in a row, no caption), so several such
  rows can share sender + minute + empty caption with **different
  seconds**. `media-missing` groups *twins* on the **exact** `sent_at`, so
  each of them would report `twin_count: 1` and the extension could not
  tell them apart. This is an API gap (§7).

## 4a. The production baseline is a phone export (minutes only)

Production's history was imported on 2026-10-09 as a `backfill` from a
WhatsApp **phone** export: about 16.5k messages, Android style (dash
separator, `M/D/YY`, 12 h clock) and **no seconds**, so every stored
`sent_at` ends in `:00`. A **Web** export (what the extension would upload)
has seconds (`[M/D/YY, h:mm:ss AM]`).

`dedupe_hash` (`backend/app/services/whatsapp/chat.py`) hashes
`sent_at.isoformat()` exactly, and `process_import` has no date filter. If
nothing changes, a Web export would hash every message differently from the
baseline (`…T14:05:33` vs `…T14:05:00`). Every overlapping message would be
stored a **second time**: the whole history, if the export has no date
range. Those duplicates would then reach the daily analysis and
`media-missing`. **#45 must fix this before the first sync.**

**Fix: truncate to the minute inside `dedupe_hash`**, i.e.
`sent_at.astimezone(PANAMA_TZ).replace(second=0, microsecond=0, tzinfo=None)`
(checked against `chat.py`):

- **Stored hashes do not change.** Every baseline row already has `:00`, so
  its hash input is identical before and after. Nothing re-imports and no
  rehash migration is needed. The CLAUDE.md warning ("changing it makes every
  stored message import again") assumes an input that changes for stored
  rows, which is not the case here. #45 must still check that this holds in
  production before shipping, with a count-only query such as
  `SELECT count(*) FROM whatsapp_messages WHERE extract(second FROM sent_at) <> 0`.
  The answer should be 0. Any row it finds (an earlier Web import) would
  import once more after the change. Rehash those rows in a migration, or
  accept and delete that one-off duplicate set; that is an operator
  decision.
- **Web and phone exports then hash the same message equally**, as long as
  sender and normalized text agree (see the caveats below). `sent_at`
  itself is still stored with the export's seconds. Only the hash drops
  them.
- **Occurrence suffix.** The parser counts `seen[key]`, where `key` is the
  hash without a suffix, in file (chat) order. With minute truncation, *k*
  identical messages (same sender, text, media flag) in one minute get `#1…#k`
  in both exports, in the same chat order. Today a Web export would give them
  distinct hashes from the seconds alone, while the phone export already
  suffixes them, so truncation makes the two **consistent**. Exports cut by
  day (or not at all) never split a minute. The counts diverge only if the
  two exports hold a different set of that minute's identical messages
  (e.g. one was deleted between exports). That is the same limit as today.
- **Merging risk.** Two genuinely different messages with the same sender,
  text, media flag and minute, distinct only by seconds, stay apart
  **within one export** through the suffix. Across exports they match
  pairwise in order, which is correct. Nothing merges that the minute-only
  baseline did not already treat the same way.
- **Caveats to test in #45** with a dry run on a recent day (counts only):
  - Text must normalize equally between the Android and Web forms. `[forwarded]`
    is stripped already. Edited and deleted stubs may differ, for example
    whether a "<This message was edited>" suffix appears.
  - Own messages must carry the same sender name in both exports.
  - The media flag must agree: `<Media omitted>` and `<image omitted>` both
    give `has_media`.

  If a day shows "new" messages that are already stored, fix the
  normalization before the first real sync.

Fallback if truncation is rejected: a server-side skip of every message at
or before the newest stored minute (`last_message_at`), keeping only messages
in that boundary minute whose hash is not stored. It is weaker: it cannot
repair gaps, and it trusts the device clock order.

## 5. Lazy loading and virtualization

- Opening the chat loads the last few dozen messages (23 rows here).
  Setting the scroller's `scrollTop = 0` loads an earlier page within about
  3 s: rows went 23 → 44 → 56 → 81. WhatsApp keeps the visible message
  anchored, so `scrollTop` jumps back down after each load.
- Older rows are **not removed** from the DOM. Their `[role=row]` and
  `[data-id]` stay, but their content is **dehydrated**
  (`[data-virtualized="true"]`, 3 nodes, fixed height). After a scroll to the
  bottom, 69 of 81 rows were placeholders. Before reading or acting on a row,
  `scrollIntoView({block: "center"})` it and wait for
  `data-virtualized="false"` (about 2 to 4 s; one document row needed a
  second try).
- **Paging back**: repeat `scrollTop = 0` and wait until the first row's
  `data-id` changes or the row count grows. Stop when the target `data-id`
  is present, or when the oldest loaded day is before the target day, or
  after N pages (a guard). The end of history shows as no growth.
- Seen live during calibration: a programmatic `scrollTop = 0` sometimes
  loads nothing. The extension (#102) then tries, in order, scrolling the
  oldest row into view after a small nudge down, and an upward `wheel`
  event on the scroller, before treating no growth as the start of the
  chat; the run counts `paging_fallback`. Which fallback works is not yet
  verified live.

### Locating (#118)

The first live run after #102 located nothing (`skip_not_found` 24,
`skip_not_loaded` 1). Most misses were #117's misdated messages (stored
on 2026-09-10, a day where those bubbles do not exist); about 6 were
genuine same-day misses. A read-only console probe in the live group
(English UI, shapes only) found: 100 rows; 10 day separators, none inside
a row, none `sticky` / `absolute`, distinct tops; every separator label
contained a digit (no word «Today» / «Yesterday» seen); 5 media rows, each
hydrated, with `author` and `tail-in`, no `tail-out`, and a `msg-meta` of
shape `9:99 A` (`h:mm PM`). So stacked separators and a duration in the
meta are not supported, and incoming run-start media rows carry an
author. What the extension does now:

- **Day of a row**, read afresh on every scan (never cached across
  scans): separators and rows in DOM order when the separators sit among
  the rows, else by layout (`match.orderDayEntries`); text rows'
  `data-pre-plain-text` dates are checked against the separator
  (`match.assignDays`), never guessing: a separator that reads one way
  only (Today / Ayer / a weekday / a written or unambiguous date) beats an
  ambiguous anchor (`9/10`, read through the guessed date order); any
  other disagreement, or a stretch after a date-like label that was not
  understood, leaves its rows with no day (skipped, `anchor_conflicts`).
  Separator labels
  may be written dates with a leading weekday and/or without the year
  («Thursday, October 8», «jueves, 8 de octubre de 2026», «Oct 8»).
- **Sender**: a row's own label is its pre-plain sender, else an
  `author` element **outside** a quoted message (`match.rowAuthor`); it is
  propagated over a run (from a tail, a day change or an unreadable row),
  forward and backward; a row whose label conflicts with the run's stops
  the spreading (each row of that run keeps only its own label); own runs
  take the label of a clean own run of the same scan
  (`match.propagateSenders`).
- **Minute**: the pre-plain time, else the **last** clock in `msg-meta`.
- **Queue**: newest first; once a day cannot be loaded, every item of that
  day or older is booked `not_loaded` without paging.
- **Diagnostics**: a `not_found` records the first funnel stage that left
  no bubble (`day_rows → media_rows → kind → minute → sender → caption →
  filename`, `match.explainMiss`) with near misses (minute ±1, whole hours
  off, unknown sender, a match on another day) and the scan's counts
  (separators parsed / unparsed + their shapes, rows, rows with no day,
  separator for the item's day). Counts, ids and shapes only.

Console probe (DevTools on the WhatsApp tab; prints shapes and counts,
never text):

```js
(() => {
  const root = document.querySelector('[data-testid="conversation-panel-messages"]');
  const shape = (t) => (t || '').trim().replace(/\p{L}+/gu, (w) => (/^\p{Lu}/u.test(w) ? 'A' : 'a')).replace(/\p{N}+/gu, '9');
  const rows = root.querySelectorAll('[role="row"]');
  const seps = [...root.querySelectorAll('span')]
    .filter((s) => !s.childElementCount && !s.closest('[role="row"]') && /\d|today|yesterday|hoy|ayer/i.test(s.textContent) && s.textContent.trim().length <= 40)
    .map((s) => ({ shape: shape(s.textContent), pos: getComputedStyle(s.parentElement).position, top: Math.round(s.getBoundingClientRect().top) }));
  const media = [...rows].filter((r) => r.querySelector('[data-testid="image-thumb"],[data-testid="document-thumb"],[data-icon="ptt-status"]')).slice(0, 5)
    .map((r) => ({ author: !!r.querySelector('[data-testid="author"]'), tailIn: !!r.querySelector('[data-icon="tail-in"]'), tailOut: !!r.querySelector('[data-icon="tail-out"]'), meta: shape([...r.querySelectorAll('[data-testid="msg-meta"]')].pop()?.textContent) }));
  return JSON.stringify({ rows: rows.length, seps, media });
})();
```

## 6. Options for #45

**(A) Export chat (text) + per-message media (what #43 built).**
The text comes from WhatsApp's own export: a format our parser already
handles (tested on the Web form), covering every kind of message
(forwarded, edited, deleted, system). Against the minutes-only phone
baseline it dedupes **only after the minute truncation in §4a**; without
that, the first sync would store the overlap twice.
Media: `media-missing` lists what lacks a file. The extension pages back to
the oldest item's day, finds each bubble by sender + `h:mm` + caption (day
from the separators), and gets the bytes: an image from its rendered `blob:`
`<img>` (or the download action if fidelity matters), a voice note or
document from the download action. Weak spots: the export dialog's delivery
(to calibrate), minute-level matching (fix in §7), and the download
mechanics.

**(B) DOM reading + a JSON message endpoint.**
This skips the export. Precision is not what decides it: the DOM's minutes
match the minutes-only baseline, and with §4a the hashes could agree.
The extension would then have to reproduce the server's text normalization,
media flag and occurrence counting in JavaScript, from markup that differs
by kind. Text is only in text bubbles. Media bubbles need inferred sender
and day. Forwarded, edited and deleted handling is unverified. Lazy loading means
scrolling through every message rather than only the files. It would also
need a new endpoint, new storage for `data-id`, and a second parser in
JavaScript. Getting kinds reliably means the internal model, which is
rejected (§3).

**Recommendation: (A)**, with the minute truncation of §4a and the small
API additions below. The export is WhatsApp's supported, complete text
format, and our parser already handles it end to end. The DOM is needed only to locate the few media
bubbles. `data-id` serves as the extension's local cursor ("last
processed") and as a server-side key once a file is matched.

## 7. API changes for #45 (beyond #43)

*Status: items 0–2 shipped with #45 (migration 0023 for `wa_key`). The
phone export turned out to be a one-time baseline with a clean cutoff (up
to 2026-10-08; from 2026-10-09 everything comes from WhatsApp Web), so the
minute truncation is the safety net for a Web export that reaches back past
the cutoff.*

0. **`dedupe_hash` at minute precision (required, before the first sync).**
   See §4a: truncate inside `dedupe_hash`, check the production
   seconds count first, and test both export forms (a synthetic Android and
   Web `.txt` of the same messages must give equal hashes, including
   same-minute repeats).
1. **Twins at minute precision (required).** `media_upload.missing_media`
   should group twins on `date_trunc('minute', sent_at)` + sender + text,
   ordered by `sent_at, id`. That way `twin_index` / `twin_count` describe
   what the extension can see. Baseline rows (`:00`) are unaffected; rows
   stored from Web exports carry seconds and need it.
   `docs/extension-api.md` already tells the extension to compare at the
   minute.
2. **Optional `wa_key` on `POST …/{run}/messages/{id}/media` (recommended).**
   This is the 32-hex `data-id`, validated as `^[0-9A-F]{32}$`, stored in a
   new nullable `whatsapp_messages.wa_key` column (migration, unique where
   not null), and returned in `media-missing` items. A message matched once
   is then never re-matched by heuristics. It also lets `409` ("already has
   a different file") be told apart from a wrong match.
3. **Zip wrapping** stays on the extension side if the export turns out to
   be a bare `.txt` (no API change).

Not needed: a per-message JSON ingest endpoint (option B).

## 8. Open questions (calibrate in #45)

- Export dialog: label path, any date range, and how the file is delivered
  (`.txt` vs `.zip`, blob anchor vs downloads API).
- Download action for voice notes and documents: the menu label (`Download`
  / «Descargar» *(unverified)*), and whether it saves through a blob anchor or
  the browser download manager. Also the saved type (voice notes expected as
  Ogg Opus).
- Whether the rendered image `blob:` holds the original bytes or a
  re-encode.
- Markers for own, forwarded, edited and deleted messages, view-once
  messages, stickers, videos and "Read more".
- The Spanish UI: labels and the `data-pre-plain-text` date/time format.
