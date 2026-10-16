// One «Sincronizar» run (docs/extension-api.md, recon option A):
//   1. GET /last; open/use the WhatsApp Web tab; select the group;
//   2. «Exportar chat» (text only) → captured in the page → zip;
//   3. upload it as a `daily` run with expects_media; wait until `done`;
//   4. GET media-missing for this run and for earlier runs within
//      `retryDays` (all pages), minus the skip book; for each, newest
//      first (up to `maxFiles`, while the run's window lasts): locate the
//      bubble, download it into a capture, upload it with its wa_key
//      (data-id); a file it cannot handle goes in the skip book (ids only)
//      for a while; once a day cannot be loaded, older items are booked
//      `not_loaded` without paging. A `not_found` records its funnel stage
//      (`nf_<stage>`, `notFound`: ids and numbers only) for «Diagnóstico»;
//   5. POST media/complete (always, once the run exists).
// Dry run: steps 1, 2 and 4's listing, locate + download, but no upload at
// all and no change to the skip book.
//
// Every step is persisted (state.js). On anything unexpected the run stops
// with a Spanish status. Never logs message content, file names or the key.

import { ApiError, createApi, describeApiError, isAllowedApiBase, isValidKey } from './api.js';
import { getMemory, getSettings, patchMemory, patchRun, setRun } from './state.js';
import { exportToZip } from '../shared/zip.js';
import { summarizeExport } from '../shared/export-text.js';
import {
  base64ToBytes,
  bump,
  chunkRanges,
  forgetSkip,
  downloadFailurePolicy,
  isSkipped,
  isWaKey,
  itemDay,
  mergeItems,
  mergeShapes,
  missEntry,
  notLoadedSkip,
  orderQueue,
  pushBounded,
  pruneSkips,
  recordSkip,
  retrySince,
  uploadOutcome,
} from '../shared/run-helpers.js';

const WA_URL = 'https://web.whatsapp.com/';
const READ_CHUNK = 4 * 1024 * 1024;
const IMPORT_TIMEOUT_MS = 20 * 60 * 1000;
const MAX_MEDIA_MB = 199;
const MEDIA_WINDOW_MS = 80 * 60 * 1000; // the server's MEDIA_WAIT is 90 min

let active = false;

class RunStop extends Error {
  constructor(message, detail) {
    super(message);
    this.detail = detail;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function isActive() {
  return active;
}

// ── Tab and content-script plumbing ──

async function send(tabId, message) {
  let response;
  try {
    response = await chrome.tabs.sendMessage(tabId, message);
  } catch {
    throw new RunStop('Se perdió el contacto con la pestaña de WhatsApp Web (¿se cerró o recargó?).');
  }
  if (!response?.ok) {
    throw new RunStop(response?.message || 'WhatsApp Web no respondió como se esperaba.', {
      code: response?.code,
      ...(response?.detail ?? {}),
    });
  }
  return response;
}

async function ping(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: 'n16:ping' });
  } catch {
    return null;
  }
}

/** The WhatsApp Web tab, always freshly (re)loaded: a reload drops any
 * capture state an earlier run left in the page (an abandoned or late
 * download, capture-main.js quarantine), and loads the content scripts if
 * the tab was open before the extension. So every run starts clean. */
async function whatsappTab() {
  const [existing] = await chrome.tabs.query({ url: `${WA_URL}*` });
  let tab = existing;
  if (!tab) {
    tab = await chrome.tabs.create({ url: WA_URL, active: true });
  } else {
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.tabs.reload(tab.id);
    await sleep(1500); // let the old page go before pinging
  }
  const end = Date.now() + 90_000;
  while (Date.now() < end) {
    const current = await chrome.tabs.get(tab.id).catch(() => null);
    if (!current) break;
    if (current.status === 'complete' && (await ping(tab.id))) return tab.id;
    await sleep(1000);
  }
  throw new RunStop('La pestaña de WhatsApp Web no cargó la extensión. Recárgala y vuelve a intentar.');
}

async function readCapture(tabId, capture) {
  const maxBytes = MAX_MEDIA_MB * 1024 * 1024;
  if (capture.size > maxBytes) {
    await send(tabId, { type: 'n16:dropCapture', id: capture.id });
    return null;
  }
  const parts = [];
  for (let offset = 0; offset < capture.size; offset += READ_CHUNK) {
    const chunk = await send(tabId, {
      type: 'n16:readCapture',
      id: capture.id,
      offset,
      length: READ_CHUNK,
    });
    parts.push(base64ToBytes(chunk.base64));
  }
  await send(tabId, { type: 'n16:dropCapture', id: capture.id });
  return new Blob(parts, { type: capture.type || 'application/octet-stream' });
}

// ── API steps ──

async function uploadText(api, zip, dateOrder) {
  const run = await api.createRun(zip.length, dateOrder);
  await patchRun({ phase: 'uploading', runId: run.id });
  const ranges = chunkRanges(zip.length, run.chunk_size, run.chunk_count);
  for (const [index, [start, end]] of ranges.entries()) {
    let attempt = 0;
    for (;;) {
      try {
        await api.putChunk(run.id, index, zip.subarray(start, end));
        break;
      } catch (err) {
        if (++attempt >= 3 || (err instanceof ApiError && err.status >= 400 && err.status < 500)) {
          throw err;
        }
        await sleep(2000 * attempt);
      }
    }
  }
  await api.completeUpload(run.id);
  return run.id;
}

async function waitForImport(api, runId) {
  const end = Date.now() + IMPORT_TIMEOUT_MS;
  while (Date.now() < end) {
    const imp = await api.getImport(runId);
    if (imp.status === 'done') return imp;
    if (imp.status === 'failed' || imp.status === 'cancelled') {
      throw new RunStop('n16chat no pudo procesar la exportación del chat.', { status: imp.status });
    }
    await patchRun({ phase: 'importing' });
    await sleep(3000);
  }
  throw new RunStop('La importación tarda demasiado; revisa «Importar WhatsApp» en n16chat.');
}

// ── Media ──

/** Which missing files this run works on: this run's new ones plus the
 * earlier runs' still missing within `retryDays` (uploaded through this
 * run, as the API allows), minus the ids in the skip book. */
async function mediaQueue(api, settings, runId, skips, now) {
  const since = retrySince(now, settings.retryDays);
  const older = await api.allMissing({ since });
  const own = runId != null ? await api.allMissing({ import_id: runId }) : { items: [] };
  const all = orderQueue(own.items, older.items);
  const queue = all.filter((item) => !isSkipped(skips, item.id, now));
  return {
    queue,
    listed: mergeItems(all).length,
    waiting: all.length - queue.length,
    truncated: Boolean(older.truncated || own.truncated),
  };
}

async function syncMedia({ api, tabId, settings, runId, queue, deadline }) {
  const group = settings.groupName;
  const live = !settings.dryRun && runId != null;
  let counts = {};
  let { skips } = await getMemory();
  const skip = async (item, reason) => {
    counts = bump(counts, `skip_${reason}`);
    if (live) {
      skips = recordSkip(skips, item.id, reason, Date.now());
      await patchMemory({ skips });
    }
    await patchRun({ counts });
  };
  let attempted = 0;
  let downloadFailures = 0; // in a row
  let notFound = [];
  let sepShapes = [];
  let oldestLoadedAge = null;
  let notLoadedDay = null; // the newest day that could not be loaded
  for (const item of queue) {
    if (item.media_kind === 'other') {
      // Never matched (match.js chooseBubble): book it without scanning.
      await skip(item, 'other_kind');
      continue;
    }
    if (notLoadedSkip(item, notLoadedDay)) {
      // The queue is newest first: an older day cannot load either.
      counts = bump(counts, 'nl_skipped_older');
      await skip(item, 'not_loaded');
      continue;
    }
    if (attempted >= settings.maxFiles) break;
    if (Date.now() > deadline) {
      counts = bump(counts, 'stopped_window');
      break;
    }
    attempted++;
    // A script error on this bubble books it (so the next run does not stall
    // on it), then stops the run as any other script error.
    const forItem = async (message) => {
      try {
        return await send(tabId, message);
      } catch (err) {
        if (err instanceof RunStop && err.detail?.code === 'script_error') {
          await skip(item, 'script_error');
        }
        throw err;
      }
    };
    const located = await forItem({
      type: 'n16:locate',
      group,
      item,
      maxPages: settings.maxPages,
    });
    if (located.paging_fallbacks) {
      // Pages that loaded only through a fallback (main.js PAGING_STEPS).
      counts = bump(counts, 'paging_fallback', located.paging_fallbacks);
    }
    if (located.diag?.sep_shapes) {
      sepShapes = mergeShapes(sepShapes, located.diag.sep_shapes);
    }
    if (located.skip === 'not_found') {
      // Diagnostics (#118): the funnel stage and numbers, ids only.
      const entry = missEntry(item.id, located);
      counts = bump(counts, `nf_${entry.stage}`);
      notFound = pushBounded(notFound, entry);
      await patchRun({ notFound, sepShapes });
    } else if (located.skip === 'not_loaded') {
      counts = bump(counts, `nl_${located.not_loaded === 'no_growth' ? 'no_growth' : 'max_pages'}`);
      if (typeof located.oldest_age === 'number') {
        oldestLoadedAge = Math.max(oldestLoadedAge ?? 0, located.oldest_age);
      }
      notLoadedDay = itemDay(item);
      await patchRun({ oldestLoadedAge, sepShapes });
    }
    if (located.skip) {
      await skip(item, located.skip);
      continue;
    }
    counts = bump(counts, 'located');
    const downloaded = await forItem({ type: 'n16:download', group, key: located.key });
    if (downloaded.skip) {
      // Booked even when the run stops here, so the next run does not stall on it.
      await skip(item, downloaded.skip);
      if (downloaded.failure) {
        downloadFailures++;
        counts = bump(counts, `failure_${downloaded.failure}`);
        await patchRun({ counts });
        if (downloadFailurePolicy(downloaded.failure, downloadFailures).stop) {
          throw new RunStop(
            downloaded.failure === 'download_capture_timeout'
              ? 'Una descarga de WhatsApp Web no llegó a tiempo; ese archivo queda para más adelante. Vuelve a sincronizar en unos minutos.'
              : 'Varias descargas seguidas fallaron; puede que WhatsApp Web haya cambiado. Hay que calibrar la extensión.',
            { code: downloaded.failure, ...(downloaded.detail ?? {}) },
          );
        }
      }
      continue;
    }
    downloadFailures = 0;
    const blob = await readCapture(tabId, downloaded.capture);
    if (!blob) {
      await skip(item, 'too_large');
      continue;
    }
    counts = bump(counts, 'captured');
    if (!live) {
      await patchRun({ counts });
      continue;
    }
    let outcome;
    try {
      const waKey = isWaKey(located.key) ? located.key : null;
      const result = await api.uploadMedia(runId, item.id, blob, downloaded.capture.name, waKey);
      outcome = uploadOutcome(result.status);
    } catch (err) {
      // Run-wide problems (network, server, key revoked / no longer admin)
      // stop the run instead of booking every remaining item.
      if (!(err instanceof ApiError) || [0, 401, 403].includes(err.status) || err.status >= 500) {
        throw err;
      }
      if (err.code === 'run_closed') {
        // The run's 90-minute window closed: the rest waits for the next run.
        counts = bump(counts, 'stopped_window');
        break;
      }
      outcome = uploadOutcome(err.status);
    }
    if (outcome === 'uploaded' || outcome === 'already') {
      counts = bump(counts, outcome);
      skips = forgetSkip(skips, item.id);
      await patchMemory({ skips });
      await patchRun({ counts });
    } else {
      await skip(item, outcome);
    }
  }
  return counts;
}

// ── The run ──

export async function startSync() {
  if (active) return;
  active = true;
  const keepAlive = setInterval(() => {
    chrome.runtime.getPlatformInfo().catch(() => {});
  }, 20_000);
  const settings = await getSettings();
  await setRun({
    phase: 'starting',
    dryRun: Boolean(settings.dryRun),
    retryDays: settings.retryDays,
    startedAt: new Date().toISOString(),
    counts: {},
    heartbeatAt: Date.now(),
  });
  let api = null;
  let runId = null;
  let mediaReady = false;
  try {
    if (!isAllowedApiBase(settings.apiBase)) {
      throw new RunStop('La dirección de la API no está permitida: revísala en «Opciones».');
    }
    if (!isValidKey(settings.token)) {
      throw new RunStop('Falta la clave de la extensión (n16x_…): ábrela en «Opciones».');
    }
    if (!settings.groupName.trim()) {
      throw new RunStop('Falta el nombre del grupo: configúralo en «Opciones».');
    }
    api = createApi(settings);
    const last = await api.last();
    await patchRun({ phase: 'opening', lastMessageAt: last.last_message_at });

    const tabId = await whatsappTab();
    await send(tabId, { type: 'n16:beginScan', dateOrder: settings.dateOrder });
    await send(tabId, { type: 'n16:selectChat', group: settings.groupName });

    await patchRun({ phase: 'exporting' });
    const exported = await send(tabId, {
      type: 'n16:exportChat',
      group: settings.groupName,
      lastMessageAt: last.last_message_at,
    });
    const blob = await readCapture(tabId, exported.capture);
    if (!blob || blob.size === 0) throw new RunStop('La exportación del chat llegó vacía o demasiado grande.');
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let zip;
    try {
      zip = exportToZip(bytes);
    } catch {
      throw new RunStop('WhatsApp Web entregó un zip sin el chat (.txt).');
    }
    const summary = zip === bytes ? null : summarizeExport(new TextDecoder().decode(bytes), last.last_message_at);
    await patchRun({
      exported: summary && {
        messages: summary.messages,
        fromSince: summary.fromSince,
        mediaFromSince: summary.mediaFromSince,
      },
      exportVia: exported.capture.via,
    });

    // The upload window: the server takes files until MEDIA_WAIT (90 min)
    // after the import finished; stop starting new ones a little earlier.
    let deadline = Date.now() + MEDIA_WINDOW_MS;
    if (!settings.dryRun) {
      runId = await uploadText(api, zip, exported.dateOrder);
      const imp = await waitForImport(api, runId);
      mediaReady = true;
      deadline = Date.now() + MEDIA_WINDOW_MS;
      await patchRun({ messages: imp.stats?.messages ?? null });
    }
    await patchRun({ phase: 'media' });
    const memory = await getMemory();
    const now = Date.now();
    const skips = pruneSkips(memory.skips, now);
    if (!settings.dryRun) await patchMemory({ skips });
    const plan = await mediaQueue(api, settings, runId, skips, now);
    await patchRun({
      mediaListed: plan.listed,
      mediaWaiting: plan.waiting,
      mediaTruncated: plan.truncated,
    });
    await syncMedia({ api, tabId, settings, runId, queue: plan.queue, deadline });

    if (runId != null) {
      await patchRun({ phase: 'completing' });
      await api.completeMedia(runId);
      mediaReady = false;
      const final = await api.getImport(runId);
      await patchRun({ importStatus: final.status, messages: final.stats?.messages ?? null });
      await patchMemory({ lastSuccessAt: new Date().toISOString() });
    }
    await patchRun({ phase: 'done', finishedAt: new Date().toISOString(), error: null });
  } catch (err) {
    const message =
      err instanceof RunStop
        ? err.message
        : err instanceof ApiError
          ? describeApiError(err)
          : 'Error inesperado de la extensión.';
    console.warn('[n16chat] sync stopped', err instanceof RunStop ? err.detail?.code : err?.name);
    await patchRun({
      phase: 'error',
      error: message,
      errorDetail: err instanceof RunStop ? err.detail ?? null : null,
      finishedAt: new Date().toISOString(),
    });
    if (api && runId != null && mediaReady) {
      // Hand the files uploaded so far to the pipeline now (the server would
      // otherwise finalize the run only after 90 minutes).
      try {
        await api.completeMedia(runId);
      } catch {
        // The server's deadline job covers it.
      }
    }
  } finally {
    clearInterval(keepAlive);
    active = false;
  }
}
