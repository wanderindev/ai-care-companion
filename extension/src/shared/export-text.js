// Counts over an exported chat .txt for the status / dry-run report (pure).
// The server's parser (backend/app/services/whatsapp/chat.py) is the one
// that matters; this only estimates "how many messages since the last
// stored one" and never keeps or reports message content.

const INVISIBLE = /[‎‏‪-‮⁦-⁩﻿]/g;
const SPACES = /[   ]/g;
const DATE = String.raw`(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})`;
const TIME = String.raw`(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([ap]\.?\s?m\.?)?`;
const BRACKETED = new RegExp(String.raw`^\[${DATE},?\s+${TIME}\]\s`, 'i');
const DASH = new RegExp(String.raw`^${DATE},?\s+${TIME}\s+-\s`, 'i');
const MEDIA = /<[^>]*(omitted|omitid[oa])>|\b(omitted|omitid[oa])\b/i;

function headers(text) {
  const found = [];
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  for (const raw of lines) {
    const line = raw.replace(INVISIBLE, '').replace(SPACES, ' ');
    const m = line.match(BRACKETED) ?? line.match(DASH);
    if (m) {
      found.push({
        parts: [m[1], m[2], m[3]],
        hour: Number(m[4]),
        minute: Number(m[5]),
        ampm: m[7] ? m[7][0].toLowerCase() : null,
        media: MEDIA.test(line),
      });
    }
  }
  return found;
}

function toKey(header, order) {
  let [a, b, c] = header.parts.map(Number);
  let year;
  let month;
  let day;
  if (header.parts[0].length === 4) [year, month, day] = [a, b, c];
  else {
    if (c < 100) c += 2000;
    year = c;
    [month, day] = order === 'dmy' ? [b, a] : [a, b];
  }
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  let hour = header.hour;
  if (header.ampm) hour = (hour % 12) + (header.ampm === 'p' ? 12 : 0);
  const pad = (n) => String(n).padStart(2, '0');
  return `${year}-${pad(month)}-${pad(day)}T${pad(hour)}:${pad(header.minute)}`;
}

/** "mdy" or "dmy" (as the server decides: an impossible month rules an
 * order out; otherwise the order whose timestamps go backwards less). */
export function dateOrder(found) {
  const numeric = found.filter((h) => h.parts[0].length !== 4);
  if (numeric.some((h) => Number(h.parts[0]) > 12)) return 'dmy';
  if (numeric.some((h) => Number(h.parts[1]) > 12)) return 'mdy';
  const backwards = (order) => {
    let count = 0;
    let previous = '';
    for (const h of numeric) {
      const key = toKey(h, order);
      if (key && key < previous) count++;
      if (key) previous = key;
    }
    return count;
  };
  return backwards('mdy') < backwards('dmy') ? 'mdy' : 'dmy';
}

/** Counts for the report: every message, and those at or after `since`
 * (an API timestamp on the Panamá clock, e.g. "2026-10-08T21:14:00-05:00",
 * compared at the minute: the server dedupes the overlap). */
export function summarizeExport(text, since) {
  const found = headers(text);
  const order = dateOrder(found);
  const sinceKey = since ? String(since).slice(0, 16) : null;
  let fromSince = 0;
  let mediaFromSince = 0;
  let newest = null;
  for (const h of found) {
    const key = toKey(h, order);
    if (!key) continue;
    if (!newest || key > newest) newest = key;
    if (!sinceKey || key >= sinceKey) {
      fromSince++;
      if (h.media) mediaFromSince++;
    }
  }
  return { messages: found.length, fromSince, mediaFromSince, newest, order };
}
