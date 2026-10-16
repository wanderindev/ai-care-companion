import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  ApiError,
  apiUrl,
  createApi,
  describeApiError,
  isAllowedApiBase,
  isValidKey,
  pendientesUrl,
} from '../src/background/api.js';
import {
  chunkRanges,
  forgetSkip,
  isSkipped,
  leftoverLine,
  mergeItems,
  pruneSkips,
  recordSkip,
  retrySince,
  SKIP_BOOK_MAX,
  diagnosticDetail,
  diagnosticLine,
  downloadFailurePolicy,
  isWaKey,
  itemDay,
  mergeShapes,
  missEntry,
  notLoadedSkip,
  orderQueue,
  pushBounded,
  skipDelay,
  isStale,
  summaryLine,
  uploadOutcome,
} from '../src/shared/run-helpers.js';

const KEY = 'n16x_' + 'a'.repeat(43);

test('apiUrl joins the base and drops empty query values', () => {
  assert.equal(
    apiUrl('https://app.example.com/api/v1/', '/imports/whatsapp/media-missing', {
      import_id: 4,
      since: undefined,
      limit: 25,
    }),
    'https://app.example.com/api/v1/imports/whatsapp/media-missing?import_id=4&limit=25',
  );
});

test('pendientesUrl points at the app, not the API', () => {
  assert.equal(pendientesUrl('https://app.example.com/api/v1'), 'https://app.example.com/pendientes');
  assert.equal(pendientesUrl('nope'), null);
});

test('isValidKey accepts only n16x_ keys', () => {
  assert.ok(isValidKey(KEY));
  assert.ok(!isValidKey('n16x_short'));
  assert.ok(!isValidKey('Bearer ' + KEY));
});

test('requests send the bearer key, no cookies, and the CSRF header on writes', async () => {
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ id: 9, chunk_size: 4, chunk_count: 1 }), {
      status: 201,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  const api = createApi({ apiBase: 'https://app.example.com/api/v1', token: KEY }, fakeFetch);
  await api.last();
  await api.createRun(10);
  await api.uploadMedia(9, 812, new Blob(['x']), 'f.ogg', 'A'.repeat(32));
  await api.createRun(10, 'mdy');
  await api.createRun(10, 'auto');
  const [get, post, media, withOrder, badOrder] = calls;
  assert.equal(JSON.parse(withOrder.init.body).date_order, 'mdy');
  assert.equal('date_order' in JSON.parse(badOrder.init.body), false);
  assert.equal(get.init.headers.Authorization, `Bearer ${KEY}`);
  assert.equal(get.init.credentials, 'omit');
  assert.equal(get.init.headers['X-Requested-With'], undefined);
  assert.equal(post.init.headers['X-Requested-With'], 'n16chat-extension');
  assert.deepEqual(JSON.parse(post.init.body), {
    mode: 'daily',
    filename: 'chat.zip',
    size: 10,
    expects_media: true,
  });
  assert.ok(media.url.endsWith('/imports/whatsapp/9/messages/812/media'));
  assert.equal(media.init.body.get('wa_key'), 'A'.repeat(32));
  assert.ok(!calls.some((c) => c.url.includes(KEY)));
});

test('errors carry the status and the server Spanish detail', async () => {
  const fakeFetch = async () =>
    new Response(JSON.stringify({ detail: 'Ese mensaje ya tiene su archivo.' }), { status: 409 });
  const api = createApi({ apiBase: 'https://app.example.com/api/v1', token: KEY }, fakeFetch);
  await assert.rejects(api.completeMedia(1), (err) => err instanceof ApiError && err.status === 409);
  assert.match(describeApiError(new ApiError(401, null)), /clave/);
  assert.equal(describeApiError(new ApiError(409, 'Detalle.')), 'Detalle.');
  assert.match(describeApiError(new Error('x')), /conectar/);
});

test('chunkRanges covers the file exactly', () => {
  assert.deepEqual(chunkRanges(10, 4, 3), [
    [0, 4],
    [4, 8],
    [8, 10],
  ]);
});

test('uploadOutcome maps the media endpoint statuses', () => {
  assert.equal(uploadOutcome(201), 'uploaded');
  assert.equal(uploadOutcome(200), 'already');
  assert.equal(uploadOutcome(409), 'conflict');
  assert.equal(uploadOutcome(422), 'wrong_type');
  assert.equal(uploadOutcome(415), 'wrong_type');
  assert.equal(uploadOutcome(500), 'failed');
});

test('a run without a heartbeat for long is stale; finished runs never are', () => {
  const now = 1_000_000;
  assert.ok(isStale({ phase: 'media', heartbeatAt: now - 120_000 }, now));
  assert.ok(!isStale({ phase: 'media', heartbeatAt: now - 10_000 }, now));
  assert.ok(!isStale({ phase: 'done', heartbeatAt: 0 }, now));
});

test('retrySince goes back the configured days, bounded to 1..30', () => {
  const now = Date.parse('2026-10-20T12:00:00Z');
  assert.equal(retrySince(now, 14), '2026-10-06T12:00:00.000Z');
  assert.equal(retrySince(now, 365), '2026-09-20T12:00:00.000Z');
  assert.equal(retrySince(now, 0), '2026-10-06T12:00:00.000Z'); // invalid → default 14
});

test('mergeItems keeps each id once, in chat order', () => {
  const a = { id: 2, sent_at: '2026-10-08T10:00:00-05:00' };
  const b = { id: 1, sent_at: '2026-10-08T10:00:00-05:00' };
  const c = { id: 9, sent_at: '2026-10-07T08:00:00-05:00' };
  assert.deepEqual(
    mergeItems([a, c], [b, a]).map((i) => i.id),
    [9, 1, 2],
  );
});

test('the skip book sets ids aside for a while, never forever, and stays bounded', () => {
  const now = 1_000_000_000;
  const day = 24 * 60 * 60 * 1000;
  let book = recordSkip({}, 5, 'not_found', now);
  book = recordSkip(book, 6, 'wrong_type', now);
  assert.ok(isSkipped(book, 5, now + day));
  assert.ok(!isSkipped(book, 5, now + 3 * day)); // a heuristic miss is retried
  assert.ok(isSkipped(book, 6, now + 20 * day)); // a server refusal waits longer
  assert.deepEqual(Object.keys(pruneSkips(book, now + 3 * day)), ['6']);
  assert.ok(!isSkipped(forgetSkip(book, 6), 6, now));
  assert.deepEqual(Object.values(book).map((e) => Object.keys(e).sort()), [
    ['reason', 'until'],
    ['reason', 'until'],
  ]);
  let big = {};
  for (let id = 0; id < SKIP_BOOK_MAX + 5; id++) big = recordSkip(big, id, 'ambiguous', now + id);
  assert.equal(Object.keys(big).length, SKIP_BOOK_MAX);
  assert.ok(!(0 in big) && SKIP_BOOK_MAX + 4 in big);
});

test('leftoverLine reports what is still without upload', () => {
  assert.equal(leftoverLine({ mediaListed: 3, counts: { uploaded: 3 } }), '');
  assert.match(
    leftoverLine({ mediaListed: 10, mediaWaiting: 2, retryDays: 14, counts: { uploaded: 3 } }),
    /^Archivos sin subir: 7 \(2 en espera.*14 días\.$/,
  );
  // A dry run uploads nothing: everything listed is still missing.
  assert.match(leftoverLine({ dryRun: true, mediaListed: 4, counts: { captured: 4 } }), /: 4/);
});

test('isAllowedApiBase keeps the key on n16chat or a local backend', () => {
  assert.ok(isAllowedApiBase('https://app.example.com/api/v1'));
  assert.ok(isAllowedApiBase('http://localhost:8010/api/v1'));
  assert.ok(isAllowedApiBase('http://127.0.0.1:5180/api/v1'));
  assert.ok(!isAllowedApiBase('http://app.example.com/api/v1'));
  assert.ok(!isAllowedApiBase('https://app.example.com.evil.example/api/v1'));
  assert.ok(!isAllowedApiBase('https://evil.example/api/v1'));
  assert.ok(!isAllowedApiBase('https://user:pw@app.example.com/api/v1'));
  assert.ok(!isAllowedApiBase('nope'));
  assert.throws(() => createApi({ apiBase: 'https://evil.example', token: KEY }));
});

test('allMissing pages on since until has_more is false', async () => {
  const pages = [
    { items: [{ id: 1, sent_at: 'T1' }, { id: 2, sent_at: 'T2' }], has_more: true },
    { items: [{ id: 2, sent_at: 'T2' }, { id: 3, sent_at: 'T3' }], has_more: false },
  ];
  const urls = [];
  const fakeFetch = async (url) => {
    urls.push(new URL(url));
    return new Response(JSON.stringify(pages.shift()), { status: 200 });
  };
  const api = createApi({ apiBase: 'https://app.example.com/api/v1', token: KEY }, fakeFetch);
  const all = await api.allMissing({ import_id: 4 });
  assert.deepEqual(all.items.map((i) => i.id), [1, 2, 3]);
  assert.equal(all.truncated, false);
  assert.equal(urls[0].searchParams.get('since'), null);
  assert.equal(urls[1].searchParams.get('since'), 'T2');
  assert.equal(urls[1].searchParams.get('import_id'), '4');
  assert.equal(urls[1].searchParams.get('limit'), '1000');
});

test('errors expose the stable X-Error-Code', async () => {
  const fakeFetch = async () =>
    new Response(JSON.stringify({ detail: 'Ya terminó el plazo.' }), {
      status: 409,
      headers: { 'X-Error-Code': 'run_closed' },
    });
  const api = createApi({ apiBase: 'https://app.example.com/api/v1', token: KEY }, fakeFetch);
  await assert.rejects(api.completeMedia(1), (err) => err.code === 'run_closed');
});

test('summaryLine reports counts only', () => {
  const live = summaryLine({ messages: { new: 12 }, counts: { uploaded: 3, skip_ambiguous: 1 } });
  assert.equal(live, 'Mensajes nuevos 12 · archivos subidos 3 · omitidos 1');
  const dry = summaryLine({
    dryRun: true,
    exported: { fromSince: 5, mediaFromSince: 2 },
    counts: { located: 2, captured: 1 },
  });
  assert.match(dry, /modo de prueba/);
});

test('orderQueue: this run and earlier leftovers, each once, newest first', () => {
  const own = [
    { id: 30, sent_at: '2026-10-09T10:00:00-05:00' },
    { id: 31, sent_at: '2026-10-09T09:00:00-05:00' },
  ];
  const older = [
    { id: 5, sent_at: '2026-10-01T08:00:00-05:00' },
    { id: 31, sent_at: '2026-10-09T09:00:00-05:00' },
  ];
  assert.deepEqual(
    orderQueue(own, older).map((i) => i.id),
    [30, 31, 5],
  );
});

test('notLoadedSkip: after a day fails to load, that day and older ones are skipped', () => {
  const at = (sent_at) => ({ id: 1, sent_at });
  assert.equal(itemDay(at('2026-10-08T23:59:00-05:00')), '2026-10-08');
  assert.ok(!notLoadedSkip(at('2026-10-01T08:00:00-05:00'), null));
  assert.ok(notLoadedSkip(at('2026-10-01T08:00:00-05:00'), '2026-10-03'));
  assert.ok(notLoadedSkip(at('2026-10-03T08:00:00-05:00'), '2026-10-03'));
  assert.ok(!notLoadedSkip(at('2026-10-04T08:00:00-05:00'), '2026-10-03'));
});

test('missEntry keeps the id, the stage and numbers only', () => {
  const entry = missEntry(42, {
    skip: 'not_found',
    miss: { stage: 'sender', n: { day_rows: 9, minute: 1, sender: 0, leak: 'Persona Uno' } },
    diag: { seps: 3, day_mode: 'dom', sep_shapes: ['A, A 9'], other: 'nombre.pdf' },
  });
  assert.deepEqual(entry, {
    id: 42,
    stage: 'sender',
    day_rows: 9,
    minute: 1,
    sender: 0,
    seps: 3,
    day_mode: 'dom',
  });
  assert.equal(missEntry(1, { skip: 'not_found' }).stage, 'unknown');
  assert.equal(missEntry(1, { miss: { stage: 'Persona Uno' } }).stage, 'unknown');
  assert.equal(missEntry(1, { miss: { stage: 'toString' } }).stage, 'unknown');
});

test('pushBounded and mergeShapes stay bounded and drop anything but shapes', () => {
  let list = [];
  for (let i = 0; i < 30; i++) list = pushBounded(list, { id: i });
  assert.equal(list.length, 25);
  assert.equal(list[24].id, 24);
  assert.deepEqual(mergeShapes(['A 9'], ['A 9', 'a a 9', 'Persona Uno', '9/9/9']), [
    'A 9',
    'a a 9',
    '9/9/9',
  ]);
  const many = mergeShapes([], ['A', 'a', '9', 'A 9', 'a 9', '9 9']);
  assert.equal(many.length, 5);
});

test('diagnosticLine and diagnosticDetail: counts by stage, nothing when no miss', () => {
  assert.equal(diagnosticLine({ counts: { located: 3 } }), '');
  const run = {
    counts: { nf_sender: 2, nf_day_rows: 1, nl_max_pages: 1, located: 4, uploaded: 4 },
    oldestLoadedAge: 6,
    notFound: [{ id: 5, stage: 'sender', minute: 1 }],
    sepShapes: ['A, A 9'],
  };
  const line = diagnosticLine(run);
  assert.match(line, /^Diagnóstico: /);
  assert.match(line, /ninguna fila del día 1/);
  assert.match(line, /remitente 2/);
  assert.match(line, /tope de páginas 1/);
  assert.match(line, /hace 6 días/);
  const detail = JSON.parse(diagnosticDetail(run));
  assert.deepEqual(detail.counts, { nf_sender: 2, nf_day_rows: 1, nl_max_pages: 1, located: 4 });
  assert.deepEqual(detail.notFound, run.notFound);
  assert.deepEqual(detail.sepShapes, ['A, A 9']);
});

test('isWaKey: only a 32-hex data-id is sent as wa_key', () => {
  assert.ok(isWaKey('0123456789ABCDEF0123456789ABCDEF'));
  assert.ok(!isWaKey('3EB0C431C26A1916E8A0')); // a shorter (iPhone-origin) id
  assert.ok(!isWaKey(null));
});

test('a failed download is booked; the run stops on a timeout or repeated failures', () => {
  assert.deepEqual(downloadFailurePolicy('download_capture_timeout', 1), { stop: true });
  assert.deepEqual(downloadFailurePolicy('download_item_missing', 1), { stop: false });
  assert.deepEqual(downloadFailurePolicy('download_capture_failed', 2), { stop: false });
  assert.deepEqual(downloadFailurePolicy('download_item_missing', 3), { stop: true });
  // A located row gone at download time (#118) counts toward the stop.
  assert.deepEqual(downloadFailurePolicy('download_row_gone', 2), { stop: false });
  assert.deepEqual(downloadFailurePolicy('download_row_gone', 3), { stop: true });
  const day = 24 * 60 * 60 * 1000;
  assert.equal(skipDelay('download_failed'), 2 * day);
  assert.equal(skipDelay('other_kind'), 30 * day); // deterministic: not retried soon
});
