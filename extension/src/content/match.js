// Pure helpers for matching a WhatsApp Web bubble to a stored message from
// GET /imports/whatsapp/media-missing (docs/extension-api.md "Matching").
// No DOM and no chrome.* here: loaded as a classic content script
// (globalThis.N16.match) and imported by the node tests.

(() => {
  const N16 = (globalThis.N16 ??= {});

  // The same marks the server strips (backend/app/services/whatsapp/chat.py).
  const INVISIBLE = /[‎‏‪-‮⁦-⁩﻿]/g;
  const ODD_SPACES = /[   ]/g;
  const CAPTION_CHARS = 200; // the API's caption cut
  const WA_KEY = /^[0-9A-F]{32}$/;

  /** The server's comparison form: NFC, invisible marks out, whitespace collapsed. */
  function normalizeText(value) {
    return String(value ?? '')
      .normalize('NFC')
      .replace(INVISIBLE, '')
      .replace(ODD_SPACES, ' ')
      .split(/\s+/)
      .filter(Boolean)
      .join(' ');
  }

  function fold(value) {
    return normalizeText(value)
      .toLowerCase()
      .normalize('NFD')
      .replace(/[̀-ͯ]/g, '');
  }

  const CLOCK = /(\d{1,2}):(\d{2})(?:\s*([ap])\.?\s*m\.?)?/g;

  /** Minutes since midnight from a bubble time ("9:41 AM", "9:41 p. m.", "21:05"). */
  function parseClock(text) {
    const m = normalizeText(text).toLowerCase().match(new RegExp(CLOCK.source));
    return m ? clockOf(m) : null;
  }

  /** The bubble's send time: the text row's data-pre-plain-text time when
   * there is one (`preMinutes`), else the **last** clock in the msg-meta
   * text (an earlier "h:mm" could be something else, e.g. a duration). */
  function metaMinutes(metaText, preMinutes = null) {
    if (preMinutes != null) return preMinutes;
    const found = [...normalizeText(metaText).toLowerCase().matchAll(CLOCK)];
    return found.length ? clockOf(found[found.length - 1]) : null;
  }

  function clockOf(m) {
    let hour = Number(m[1]);
    const minute = Number(m[2]);
    if (minute > 59) return null;
    if (m[3]) {
      if (hour < 1 || hour > 12) return null;
      hour = (hour % 12) + (m[3] === 'p' ? 12 : 0);
    } else if (hour > 23) {
      return null;
    }
    return hour * 60 + minute;
  }

  /** Wall-clock day and minute of an API timestamp, read off the string
   * ("2026-10-08T15:47:00-05:00" → {date: "2026-10-08", minutes: 947}),
   * never through the device's time zone. */
  function itemLocal(sentAt) {
    const m = String(sentAt ?? '').match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/);
    if (!m) return null;
    return { date: m[1], minutes: Number(m[2]) * 60 + Number(m[3]) };
  }

  function isoDate(year, month, day) {
    const d = new Date(Date.UTC(year, month - 1, day));
    if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) {
      return null;
    }
    return d.toISOString().slice(0, 10);
  }

  function addDays(iso, days) {
    const d = new Date(`${iso}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }

  function weekday(iso) {
    return new Date(`${iso}T00:00:00Z`).getUTCDay();
  }

  function daysBetween(fromIso, toIso) {
    return Math.round(
      (Date.parse(`${toIso}T00:00:00Z`) - Date.parse(`${fromIso}T00:00:00Z`)) / 86_400_000,
    );
  }

  /** Index of a month or weekday word in `lists`: a full name, or a
   * prefix of at least three letters ("oct", "sept", "mié"); -1 if none. */
  function nameIndex(lists, word) {
    if (!word) return -1;
    const exact = lists.findIndex((names) => names.map(fold).includes(word));
    if (exact >= 0 || word.length < 3) return exact;
    return lists.findIndex((names) => names.map(fold).some((name) => name.startsWith(word)));
  }

  /** A date shown without its year: the latest such date not after today. */
  function withYear(month, day, year, today) {
    if (year) return isoDate(year, month, day);
    const current = Number(today.slice(0, 4));
    const date = isoDate(current, month, day);
    if (!date) return null;
    return date <= today ? date : isoDate(current - 1, month, day);
  }

  /** A written date, already folded ("october 8, 2026", "8 de octubre de
   * 2026", "oct 8", "8 oct 2026"), or null. */
  function writtenDate(text, today, labels) {
    let m = text.match(/^([a-z]+) (\d{1,2})(?:,? (\d{4}))?$/);
    if (m) {
      const month = nameIndex(labels.months, m[1]);
      if (month >= 0) return withYear(month + 1, Number(m[2]), Number(m[3] ?? 0), today);
    }
    m = text.match(/^(\d{1,2}) (?:de )?([a-z]+)(?:,? (?:de )?(\d{4}))?$/);
    if (m) {
      const month = nameIndex(labels.months, m[2]);
      if (month >= 0) return withYear(month + 1, Number(m[1]), Number(m[3] ?? 0), today);
    }
    return null;
  }

  /** The ISO date a day separator stands for, or null when it is not one.
   * `today` is the device's ISO date; `labels` is N16.LABELS; `dateOrder`
   * ("mdy" / "dmy") decides an ambiguous numeric date. A written date may
   * lack its year, and may start with a weekday ("Thursday, October 8,
   * 2026"), which must then fit the date. */
  function parseDayLabel(label, today, labels, dateOrder = 'mdy') {
    const text = fold(label).replace(/([a-z])\./g, '$1');
    if (!text || text.length > 40) return null;
    if (labels.today.map(fold).includes(text)) return today;
    if (labels.yesterday.map(fold).includes(text)) return addDays(today, -1);
    const day = labels.weekdays.findIndex((names) => names.map(fold).includes(text));
    if (day >= 0) {
      // A weekday label covers the days before yesterday within the week.
      for (let back = 2; back <= 7; back++) {
        const candidate = addDays(today, -back);
        if (weekday(candidate) === day) return candidate;
      }
      return null;
    }
    const numeric = text.match(/^(\d{1,4})[/.-](\d{1,2})[/.-](\d{1,4})$/);
    if (numeric) {
      let [a, b, c] = numeric.slice(1).map(Number);
      if (numeric[1].length === 4) return isoDate(a, b, c);
      if (c < 100) c += 2000;
      if (a > 12) return isoDate(c, b, a);
      if (b > 12) return isoDate(c, a, b);
      return dateOrder === 'dmy' ? isoDate(c, b, a) : isoDate(c, a, b);
    }
    const written = writtenDate(text, today, labels);
    if (written) return written;
    // "thursday, october 8, 2026" / "jueves, 8 de octubre": the weekday must fit.
    const lead = text.match(/^([a-z]+),? (.+)$/);
    if (lead) {
      const named = nameIndex(labels.weekdays, lead[1]);
      if (named < 0) return null;
      const rest = parseDayLabel(lead[2], today, labels, dateOrder);
      return rest && weekday(rest) === named ? rest : null;
    }
    return null;
  }

  /** The structure of a label that was not understood, for diagnostics:
   * each word becomes "A" (capitalized) or "a", each number "9"
   * ("Thursday, October 8" → "A, A 9"). No letter or digit survives. */
  function labelShape(label) {
    return normalizeText(label)
      .replace(/\p{L}+/gu, (w) => (/^\p{Lu}/u.test(w) ? 'A' : 'a'))
      .replace(/\p{N}+/gu, '9')
      .replace(/[^Aa9 ,./:-]/g, '?')
      .slice(0, 24);
  }

  /** Whether an unparsed leaf label could be a day separator worth
   * reporting (short, with a digit). */
  function dateLike(label) {
    const text = normalizeText(label);
    return text.length > 0 && text.length <= 40 && /\d/.test(text);
  }

  /** A text row's data-pre-plain-text ("[9:41 AM, 10/8/2026] Name: "):
   * its day (ISO), minute and sender, each null when not readable. */
  function parsePrePlain(pre, today, labels, dateOrder = 'mdy') {
    const m = String(pre ?? '').match(/^\[([^\]]*)\]\s*(.*?):\s*$/);
    if (!m) return { date: null, ambiguous: false, minutes: null, sender: null };
    const parts = m[1].split(',').map((p) => p.trim());
    const clockPart = parts.find((p) => /\d:\d{2}/.test(p));
    const datePart = parts.find((p) => /\d[/.-]\d/.test(p));
    const date = datePart ? parseDayLabel(datePart, today, labels, dateOrder) : null;
    return {
      date,
      // Read through the guessed date order: it may be day/month swapped.
      ambiguous: Boolean(date && dateAmbiguous(datePart)),
      minutes: clockPart ? parseClock(clockPart) : null,
      sender: m[2] || null,
    };
  }

  /** A row's own sender label: the pre-plain sender when there is one,
   * else its one author label (`authorLabels`: the row's author elements
   * outside a quoted message); two different labels → null. */
  function rowAuthor(preSender, authorLabels = []) {
    const pre = normalizeText(preSender);
    if (pre) return pre;
    const labels = [...new Set(authorLabels.map(normalizeText).filter(Boolean))];
    return labels.length === 1 ? labels[0] : null;
  }

  /** Whether a day label reads two ways: a numeric date whose first two
   * numbers are both ≤ 12 and differ ("9/10/2026"). Relative labels
   * (Today / Ayer / a weekday), written months and "13/10/2026" are not. */
  function dateAmbiguous(label) {
    const m = normalizeText(label).match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{1,4})$/);
    if (!m) return false;
    const a = Number(m[1]);
    const b = Number(m[2]);
    return a <= 12 && b <= 12 && a !== b;
  }

  // ── Day and sender attribution (pure; main.js reads the DOM) ──

  /** How to order a scan's separators and rows: by DOM order when the
   * separators sit among the rows (some row before the last separator and
   * some row after the first), else by layout (`top`), the recon's
   * fallback for separators kept in an overlay. `entries` are in DOM order:
   * {sep: true, top} or {row: true, top}. Returns {mode, ordered}. */
  function orderDayEntries(entries) {
    const seps = entries.map((e, i) => (e.sep ? i : -1)).filter((i) => i >= 0);
    const rows = entries.map((e, i) => (e.row ? i : -1)).filter((i) => i >= 0);
    if (!seps.length || !rows.length) return { mode: 'dom', ordered: entries };
    const interleaved = rows[0] < seps[seps.length - 1] && rows[rows.length - 1] > seps[0];
    if (interleaved) return { mode: 'dom', ordered: entries };
    const ordered = entries
      .map((e, i) => ({ e, i }))
      .sort((a, b) => (a.e.top ?? 0) - (b.e.top ?? 0) || a.i - b.i)
      .map(({ e }) => e);
    return { mode: 'layout', ordered };
  }

  /** The day of each row of one scan. `entries` are ordered (see
   * orderDayEntries):
   *   {sep: true, date, ambiguous} a parsed separator (`ambiguous`: a
   *     numeric label that reads two ways, dateAmbiguous);
   *   {sep: true, date: null, unparsed: true} a date-like label that was
   *     not understood (it still ends the stretch before it);
   *   {row: true, anchor, ambiguous} a row, `anchor` being its pre-plain
   *     date (text rows) or null.
   * Never a guessed day — a wrong day can attach one message's file to
   * another, a null only skips:
   *   - a stretch after an unparsed separator: every row null;
   *   - anchors that all agree with the separator, or none: its date;
   *   - a separator that reads one way only (Today / Ayer / a weekday / a
   *     written or unambiguous date) against anchors that are all
   *     ambiguous: the separator wins;
   *   - any other disagreement (anchors among themselves, or with the
   *     separator): every row of the stretch null, counted in `conflicts`;
   *   - rows before any separator: their single unambiguous anchor date,
   *     else null.
   * Always the scan's own reading, never a date cached from an earlier
   * scan. Returns {days (one per row, in order), conflicts}. */
  function assignDays(entries) {
    const segments = [{ lead: true, date: null, rows: [] }];
    for (const entry of entries) {
      if (entry.sep) {
        segments.push({
          date: entry.date ?? null,
          ambiguous: Boolean(entry.ambiguous),
          unparsed: Boolean(entry.unparsed || !entry.date),
          rows: [],
        });
      } else if (entry.row) {
        segments[segments.length - 1].rows.push(entry);
      }
    }
    const dayOf = new Map();
    let conflicts = 0;
    for (const seg of segments) {
      const anchored = seg.rows.filter((r) => r.anchor);
      const anchors = [...new Set(anchored.map((r) => r.anchor))];
      let date = null;
      if (seg.lead) {
        const sure = anchored.every((r) => !r.ambiguous);
        if (anchors.length === 1 && sure) date = anchors[0];
        else if (anchors.length > 1) conflicts++;
      } else if (!seg.unparsed) {
        const disagree = anchored.filter((r) => r.anchor !== seg.date);
        if (!disagree.length) date = seg.date;
        else if (!seg.ambiguous && disagree.every((r) => r.ambiguous)) date = seg.date;
        else conflicts++;
      }
      for (const row of seg.rows) dayOf.set(row, date);
    }
    return { days: entries.filter((e) => e.row).map((row) => dayOf.get(row)), conflicts };
  }

  /** The sender of each bubble of a scan (chat order). A run starts at a
   * tail (`runStart` incoming, `outgoing` own), at a day change or after
   * an unreadable row; every bubble of a run gets the run's author label,
   * found anywhere in it (forward and backward). A row whose own label
   * differs from the run's (no tail between them: the run's edges were
   * misread, or a label is not the bubble's own) stops the spreading: in
   * that run each row keeps only its own label, the others get null. An
   * own run without a label gets the operator's name: `ownName`, else the
   * one label seen on clean own runs of this scan (when exactly one).
   * Unreadable rows get null.
   * infos: [{author, runStart, outgoing, unreadable, date}]. */
  function propagateSenders(infos, ownName = null) {
    const runs = [];
    let run = null;
    let prev = null;
    for (const [index, info] of infos.entries()) {
      const fresh =
        !run ||
        info.unreadable ||
        prev?.unreadable ||
        info.runStart ||
        info.outgoing ||
        info.date !== prev?.date;
      if (fresh) {
        run = {
          own: Boolean(info.outgoing),
          author: null,
          conflict: false,
          members: [],
          unreadable: info.unreadable,
        };
        runs.push(run);
      }
      if (info.author && run.author && info.author !== run.author) run.conflict = true;
      if (info.author && !run.author) run.author = info.author;
      run.members.push(index);
      prev = info;
    }
    const clean = runs.filter((r) => !r.conflict && !r.unreadable);
    const ownLabels = new Set(clean.filter((r) => r.own && r.author).map((r) => r.author));
    const own = ownName || (ownLabels.size === 1 ? [...ownLabels][0] : null);
    const senders = new Array(infos.length).fill(null);
    for (const r of runs) {
      if (r.unreadable) continue;
      for (const index of r.members) {
        if (r.conflict) senders[index] = infos[index].author || null;
        else senders[index] = r.author ?? (r.own ? own : null);
      }
    }
    return senders;
  }

  /** Whether a bubble's caption is the stored message's (the API cuts at 200). */
  function captionMatches(caption, domText) {
    const stored = normalizeText(caption);
    const seen = normalizeText(domText);
    if (stored === seen) return true;
    return String(caption ?? '').length >= CAPTION_CHARS && stored !== '' && seen.startsWith(stored);
  }

  function senderMatches(sender, domSender) {
    return sender != null && domSender != null && normalizeText(sender) === normalizeText(domSender);
  }

  /** Whether a bubble's DOM kind fits the stored media kind. */
  function kindMatches(mediaKind, domKind) {
    if (!domKind) return false;
    if (mediaKind === 'other') return true;
    if (mediaKind === 'pdf') return domKind === 'document';
    return mediaKind === domKind;
  }

  /** The file name inside a document bubble's title ('View "x.pdf"'). */
  function filenameFromTitle(title) {
    const m = String(title ?? '').match(/"(.+)"/);
    return m ? m[1] : null;
  }

  /** Does a bubble (read from the DOM) match a media-missing item?
   * bubble: {date, minutes, sender, caption, kind, filename}. */
  function bubbleMatches(item, bubble) {
    const at = itemLocal(item.sent_at);
    if (!at || bubble.date !== at.date || bubble.minutes !== at.minutes) return false;
    if (!senderMatches(item.sender, bubble.sender)) return false;
    if (!kindMatches(item.media_kind, bubble.kind)) return false;
    if (item.media_filename && bubble.filename != null) {
      if (normalizeText(item.media_filename) !== normalizeText(bubble.filename)) return false;
    }
    return captionMatches(item.caption, bubble.caption);
  }

  /** Which of the matching bubbles (chat order) is the item, or why none.
   * Never guesses: a different number of twins than the server counts is
   * skipped. */
  function pickCandidate(item, candidates) {
    const count = item.twin_count ?? 1;
    if (candidates.length === 0) return { skip: 'not_found' };
    if (candidates.length !== count) return { skip: 'ambiguous' };
    const chosen = candidates[item.twin_index ?? 0];
    return chosen ? { candidate: chosen } : { skip: 'ambiguous' };
  }

  /** The bubble (of one day's scan, chat order) that is `item`, or why
   * none. The server groups twins by minute + sender + caption + media
   * kind, so the candidates are the bubbles matching all of those (kind
   * included). A wrong match is worse than a skip: when some bubble of the
   * day could not be read, skip unless that cannot change the count. */
  function chooseBubble(item, bubbles) {
    // An unknown export marker: any bubble kind would fit and its real
    // bubble may be one this script does not recognize, so never guess.
    if (item.media_kind === 'other') return { skip: 'other_kind' };
    const unreadable = bubbles.some((b) => b.unreadable);
    const candidates = bubbles.filter((b) => !b.unreadable && b.kind && bubbleMatches(item, b));
    if (unreadable) return { skip: 'unreadable' };
    return pickCandidate(item, candidates);
  }

  // The not_found funnel, in order: the first stage that leaves no bubble
  // is the reason reported (run.js counts it as nf_<stage>).
  const MISS_STAGES = ['day_rows', 'media_rows', 'kind', 'minute', 'sender', 'caption', 'filename'];

  /** Why `item` matched no bubble of its day (counts only, never content).
   * `bubbles` are the day's (as chooseBubble gets them); `others` are rows
   * read earlier and now attributed to another day. Returns {stage, n}:
   * `n` holds the count left after each stage plus near misses —
   * `sender_null` (minute fits, sender unknown), `minute_pm1` (kind fits,
   * one minute off), `minute_tz` (kind fits, off by whole hours: a time
   * zone) and `other_day` (a full match on another day). */
  function explainMiss(item, bubbles, others = []) {
    const at = itemLocal(item.sent_at);
    const n = {};
    const sameFile = (b) =>
      !item.media_filename ||
      b.filename == null ||
      normalizeText(item.media_filename) === normalizeText(b.filename);
    let left = bubbles.filter((b) => !b.unreadable);
    n.day_rows = bubbles.length;
    left = left.filter((b) => b.kind);
    n.media_rows = left.length;
    const kinds = left.filter((b) => kindMatches(item.media_kind, b.kind));
    n.kind = kinds.length;
    const minute = at ? kinds.filter((b) => b.minutes === at.minutes) : [];
    n.minute = minute.length;
    n.sender_null = minute.filter((b) => b.sender == null).length;
    const sender = minute.filter((b) => senderMatches(item.sender, b.sender));
    n.sender = sender.length;
    const caption = sender.filter((b) => captionMatches(item.caption, b.caption));
    n.caption = caption.length;
    n.filename = caption.filter(sameFile).length;
    const off = (b) => (at && b.minutes != null ? b.minutes - at.minutes : null);
    n.minute_pm1 = kinds.filter((b) => Math.abs(off(b) ?? 0) === 1).length;
    n.minute_tz = kinds.filter((b) => off(b) && off(b) % 60 === 0).length;
    n.other_day = at
      ? others.filter(
          (b) =>
            !b.unreadable &&
            b.date !== at.date &&
            kindMatches(item.media_kind, b.kind) &&
            b.minutes === at.minutes &&
            senderMatches(item.sender, b.sender) &&
            captionMatches(item.caption, b.caption) &&
            sameFile(b),
        ).length
      : 0;
    const stage = MISS_STAGES.find((s) => n[s] === 0) ?? 'none';
    return { stage, n };
  }

  /** Whether a capture message answers the capture armed now (`pendingId`). */
  function isCaptureFor(pendingId, data) {
    return (
      typeof pendingId === 'string' && data?.source === 'n16chat-capture' && data.id === pendingId
    );
  }

  function isWaKey(value) {
    return WA_KEY.test(String(value ?? ''));
  }

  /** The export's custom date range (ISO dates for WhatsApp's date
   * inputs): from the Panamá day of the newest stored message to today.
   * The API returns `last_message_at` on the Panamá clock (-05:00, no DST),
   * so its date part is that day. null (nothing stored yet) = export all. */
  function exportRange(lastMessageAt, today) {
    const from = /^\d{4}-\d{2}-\d{2}/.exec(String(lastMessageAt ?? ''))?.[0];
    if (!from || !/^\d{4}-\d{2}-\d{2}$/.test(String(today))) return null;
    return { from: from <= today ? from : today, to: today };
  }

  /** UI labels for a stop report (run state, popup): only labels matching
   * one of `known` (N16.LABELS.known), deduplicated, at most `max`; the
   * rest are only counted, since an unknown label may carry a sender or a
   * file name. */
  function reportLabels(labels, known, max = 12) {
    const kept = [];
    let unknown = 0;
    for (const raw of labels) {
      const label = normalizeText(raw);
      if (!label) continue;
      if (label.length <= 40 && known.some((re) => re.test(label))) {
        if (!kept.includes(label)) kept.push(label);
      } else {
        unknown++;
      }
    }
    return { labels: kept.slice(0, max), unknown_labels: unknown };
  }

  /** One page back in the message list (recon §5): try each way of asking
   * WhatsApp for older messages until one makes the list grow. A plain
   * `scrollTop = 0` sometimes loads nothing (seen live), so the later steps
   * are fallbacks. `apply(step)` performs a step, `grew(step)` waits for
   * growth after it. Returns the step that worked, or null (the beginning
   * of the chat, or a load that never came). */
  async function pageBack(steps, apply, grew) {
    for (const step of steps) {
      await apply(step);
      if (await grew(step)) return step;
    }
    return null;
  }

  N16.match = {
    reportLabels,
    pageBack,
    normalizeText,
    parseClock,
    metaMinutes,
    itemLocal,
    parseDayLabel,
    labelShape,
    dateLike,
    parsePrePlain,
    dateAmbiguous,
    rowAuthor,
    daysBetween,
    orderDayEntries,
    assignDays,
    propagateSenders,
    explainMiss,
    MISS_STAGES,
    captionMatches,
    senderMatches,
    kindMatches,
    filenameFromTitle,
    bubbleMatches,
    pickCandidate,
    chooseBubble,
    isCaptureFor,
    isWaKey,
    addDays,
    exportRange,
  };
})();
