// Pure helpers of the sync run (tested with node --test).

export const TERMINAL_PHASES = new Set(['idle', 'done', 'error', 'interrupted']);
export const STALE_MS = 90_000; // no heartbeat for this long = the worker died

/** [start, end) byte ranges of the chunked upload. */
export function chunkRanges(size, chunkSize, count) {
  const ranges = [];
  for (let i = 0; i < count; i++) {
    ranges.push([i * chunkSize, Math.min(size, (i + 1) * chunkSize)]);
  }
  return ranges;
}

/** How a media upload response counts in the run report. */
export function uploadOutcome(status) {
  if (status === 201) return 'uploaded';
  if (status === 200) return 'already';
  if (status === 409) return 'conflict';
  if (status === 413) return 'too_large';
  if (status === 415 || status === 422) return 'wrong_type';
  if (status === 404) return 'gone';
  return 'failed';
}

/** A run whose worker stopped without finishing. */
export function isStale(run, now = Date.now()) {
  return !TERMINAL_PHASES.has(run?.phase ?? 'idle') && now - (run.heartbeatAt ?? 0) > STALE_MS;
}

export function base64ToBytes(base64) {
  const binary = atob(base64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

// ── Earlier runs' missing files (retried while recent) ──

export const RETRY_DAYS_DEFAULT = 14;
export const RETRY_DAYS_MAX = 30;
const DAY_MS = 24 * 60 * 60 * 1000;

/** `since` for media-missing: `days` back from now (UTC ISO; the API
 * reads the offset). Clamped to 1..RETRY_DAYS_MAX. */
export function retrySince(now, days) {
  const d = Math.max(1, Math.min(RETRY_DAYS_MAX, Number(days) || RETRY_DAYS_DEFAULT));
  return new Date(now - d * DAY_MS).toISOString();
}

const chatOrder = (a, b) =>
  a.sent_at === b.sent_at ? a.id - b.id : a.sent_at < b.sent_at ? -1 : 1;

/** media-missing items from several listings, once each, in chat order. */
export function mergeItems(...lists) {
  const byId = new Map();
  for (const list of lists) for (const item of list) byId.set(item.id, item);
  return [...byId.values()].sort(chatOrder);
}

/** The work order: this run's items and the earlier runs' leftovers, each
 * id once, **newest first** — the newest need the least paging back, and
 * once a day cannot be loaded every older item is skipped without paging
 * (see notLoadedSkip). */
export function orderQueue(runItems, olderItems) {
  return mergeItems(runItems, olderItems).reverse();
}

/** The Panamá day (ISO) of a media-missing item, read off the string. */
export function itemDay(item) {
  return /^\d{4}-\d{2}-\d{2}/.exec(String(item?.sent_at ?? ''))?.[0] ?? null;
}

/** Whether `item` is skipped without paging because an item of day
 * `failedDay` (or a newer one) could not be loaded: its day is no newer,
 * so paging back to it would fail the same way. */
export function notLoadedSkip(item, failedDay) {
  const day = itemDay(item);
  return Boolean(failedDay && day && day <= failedDay);
}

const WA_KEY = /^[0-9A-F]{32}$/;

/** Only a 32-hex data-id is sent as wa_key (others, e.g. shorter ids of
 * messages sent from an iPhone, would be refused with 422). */
export function isWaKey(value) {
  return WA_KEY.test(String(value ?? ''));
}

// A download that failed for one bubble is booked and the run goes on,
// except: after a capture *timeout* (the page may still save that file
// later; capture-main.js quarantines it, and stopping keeps the rest of
// this run clear of it), and after MAX_DOWNLOAD_FAILURES failures in a row
// (more likely a UI change than bad bubbles). A located row that is gone
// at download time (`download_row_gone`) counts as such a failure too.
export const MAX_DOWNLOAD_FAILURES = 3;

export function downloadFailurePolicy(failure, consecutive) {
  if (failure === 'download_capture_timeout') return { stop: true };
  return { stop: consecutive >= MAX_DOWNLOAD_FAILURES };
}

// The skip book: message ids (only ids) that a run could not handle, and
// until when later runs leave them alone. A heuristic miss (bubble not
// found, ambiguous twins, unreadable rows) is retried after a couple of
// days; a refusal by the server (wrong type, too large, conflict) is left
// for the whole retry window.
export const SKIP_BOOK_MAX = 1000;
const RETRY_LATER_MS = 2 * DAY_MS;
const REFUSED_MS = RETRY_DAYS_MAX * DAY_MS;
// Deterministic outcomes: retrying soon would give the same answer.
const REFUSED = new Set(['conflict', 'wrong_type', 'too_large', 'gone', 'other_kind']);

export function skipDelay(reason) {
  return REFUSED.has(reason) ? REFUSED_MS : RETRY_LATER_MS;
}

export function pruneSkips(book, now) {
  const out = {};
  for (const [id, entry] of Object.entries(book ?? {})) if (entry.until > now) out[id] = entry;
  return out;
}

export function isSkipped(book, id, now) {
  const entry = book?.[id];
  return Boolean(entry && entry.until > now);
}

/** The book with `id` set aside for `reason`; bounded to SKIP_BOOK_MAX
 * entries (those expiring first are dropped). */
export function recordSkip(book, id, reason, now) {
  const next = { ...pruneSkips(book, now), [id]: { until: now + skipDelay(reason), reason } };
  const ids = Object.keys(next);
  if (ids.length <= SKIP_BOOK_MAX) return next;
  ids.sort((a, b) => next[a].until - next[b].until);
  for (const old of ids.slice(0, ids.length - SKIP_BOOK_MAX)) delete next[old];
  return next;
}

export function forgetSkip(book, id) {
  const next = { ...(book ?? {}) };
  delete next[id];
  return next;
}

export function bump(counts, key, by = 1) {
  return { ...counts, [key]: (counts[key] ?? 0) + by };
}

/** One-line Spanish summary of a finished run's counts. */
export function summaryLine(run) {
  const c = run.counts ?? {};
  const parts = [];
  if (run.dryRun) {
    if (run.exported) {
      parts.push(`Exportación: ${run.exported.fromSince} mensajes desde la última fecha`);
      parts.push(`${run.exported.mediaFromSince} con archivo`);
    }
    parts.push(`archivos localizados ${c.located ?? 0}, capturados ${c.captured ?? 0}`);
    parts.push('nada se subió (modo de prueba)');
  } else {
    if (run.messages) parts.push(`Mensajes nuevos ${run.messages.new ?? 0}`);
    parts.push(`archivos subidos ${c.uploaded ?? 0}`);
  }
  const skipped = Object.entries(c)
    .filter(([key]) => key.startsWith('skip_'))
    .reduce((sum, [, n]) => sum + n, 0);
  if (skipped) parts.push(`omitidos ${skipped}`);
  return parts.join(' · ');
}

// ── Locate diagnostics (#118): counts and server message ids only ──

export const NOT_FOUND_MAX = 25;
const SHAPES_MAX = 5;

/** One `notFound` entry of the run state from a locate answer: the
 * message id, the funnel stage and numbers only (anything else the page
 * sent is dropped here). */
export function missEntry(id, located) {
  const stage = located?.miss?.stage;
  const entry = { id, stage: Object.hasOwn(STAGE_LABELS, stage ?? '') ? stage : 'unknown' };
  const numbers = { ...(located?.miss?.n ?? {}), ...(located?.diag ?? {}) };
  for (const [key, value] of Object.entries(numbers)) {
    if (typeof value === 'number' && Number.isFinite(value)) entry[key] = value;
    else if (key === 'day_mode' && (value === 'dom' || value === 'layout')) entry[key] = value;
  }
  return entry;
}

/** `list` plus `entry`, keeping at most `max` (the first ones). */
export function pushBounded(list, entry, max = NOT_FOUND_MAX) {
  const current = list ?? [];
  return current.length >= max ? current : [...current, entry];
}

/** Unparsed separator shapes (match.labelShape: no letter or digit),
 * deduplicated, at most SHAPES_MAX; anything else is dropped. */
export function mergeShapes(known, shapes) {
  const out = [...(known ?? [])];
  for (const shape of shapes ?? []) {
    if (typeof shape !== 'string' || !/^[Aa9 ,./:?-]{1,24}$/.test(shape)) continue;
    if (!out.includes(shape) && out.length < SHAPES_MAX) out.push(shape);
  }
  return out;
}

const STAGE_LABELS = {
  day_rows: 'ninguna fila del día',
  media_rows: 'sin archivos en el día',
  kind: 'tipo',
  minute: 'minuto',
  sender: 'remitente',
  caption: 'texto',
  filename: 'nombre de archivo',
  none: 'otro',
  unknown: 'sin detalle',
};

/** One-line Spanish «Diagnóstico» of a run's locate misses, or '' when
 * there is nothing to show. */
export function diagnosticLine(run) {
  const c = run.counts ?? {};
  const parts = [];
  const stages = Object.keys(STAGE_LABELS)
    .filter((stage) => c[`nf_${stage}`])
    .map((stage) => `${STAGE_LABELS[stage]} ${c[`nf_${stage}`]}`);
  if (stages.length) parts.push(`no encontrados por etapa: ${stages.join(', ')}`);
  const notLoaded = [
    c.nl_max_pages ? `tope de páginas ${c.nl_max_pages}` : '',
    c.nl_no_growth ? `el chat no cargó más ${c.nl_no_growth}` : '',
    c.nl_skipped_older ? `más antiguos sin intentar ${c.nl_skipped_older}` : '',
  ].filter(Boolean);
  if (notLoaded.length) parts.push(`no cargados: ${notLoaded.join(', ')}`);
  if (run.oldestLoadedAge != null) {
    parts.push(`día más antiguo cargado: hace ${run.oldestLoadedAge} días`);
  }
  if (c.located) parts.push(`localizados ${c.located}`);
  if (!stages.length && !notLoaded.length) return '';
  return `Diagnóstico: ${parts.join(' · ')}`;
}

/** The «Diagnóstico» detail to copy into a report: the nf_/nl_ counts,
 * the bounded `notFound` list, separator shapes and the oldest day's age
 * (all counts, ids and shapes; JSON). */
export function diagnosticDetail(run) {
  const c = run.counts ?? {};
  const counts = Object.fromEntries(
    Object.entries(c).filter(([key]) => /^(nf_|nl_|skip_|located$|paging_fallback$)/.test(key)),
  );
  return JSON.stringify({
    counts,
    oldestLoadedAge: run.oldestLoadedAge ?? null,
    sepShapes: run.sepShapes ?? [],
    notFound: run.notFound ?? [],
  });
}

/** Files still without upload after a run (counts only): every missing
 * file listed, minus those this run stored. */
export function leftover(run) {
  const c = run.counts ?? {};
  const listed = run.mediaListed ?? 0;
  const stored = run.dryRun ? 0 : (c.uploaded ?? 0) + (c.already ?? 0);
  return {
    left: Math.max(0, listed - stored),
    waiting: run.mediaWaiting ?? 0,
    days: run.retryDays ?? RETRY_DAYS_DEFAULT,
  };
}

/** One-line Spanish note on what is left, or '' when nothing is. */
export function leftoverLine(run) {
  const { left, waiting, days } = leftover(run);
  if (!left) return '';
  let line = `Archivos sin subir: ${left}`;
  if (waiting) line += ` (${waiting} en espera tras no poder subirlos)`;
  return `${line}. Se reintentan en próximas sincronizaciones mientras tengan menos de ${days} días.`;
}

/**
 * Whether a runtime message comes from one of the extension's own pages
 * (popup, options). The options page opens in a tab, so `sender.tab` is
 * set for it too: judge by the sender's URL instead. Content scripts on
 * web.whatsapp.com have the page's URL and are refused.
 */
export function isOwnPage(sender, runtimeId, extensionOrigin) {
  if (!sender || sender.id !== runtimeId) return false;
  const url = typeof sender.url === 'string' ? sender.url : '';
  return url.startsWith(`${extensionOrigin}/`);
}
