import { test } from 'node:test';
import assert from 'node:assert/strict';

// Classic content scripts: they register on globalThis.N16.
await import('../src/content/selectors.js');
await import('../src/content/match.js');
const { LABELS, SELECTORS } = globalThis.N16;
const M = globalThis.N16.match;

test('normalizeText mirrors the server (invisible marks, odd spaces, collapse)', () => {
  assert.equal(M.normalizeText('‎ Hola mundo \n  ñ '), 'Hola mundo ñ');
  assert.equal(M.normalizeText('José'), 'José');
});

test('parseClock reads 12 h (English and Spanish) and 24 h bubble times', () => {
  assert.equal(M.parseClock('9:41 AM'), 9 * 60 + 41);
  assert.equal(M.parseClock('12:05 AM'), 5);
  assert.equal(M.parseClock('12:05 PM'), 12 * 60 + 5);
  assert.equal(M.parseClock('9:41 p. m.'), 21 * 60 + 41);
  assert.equal(M.parseClock('Edited 3:07 pm'), 15 * 60 + 7);
  assert.equal(M.parseClock('21:05'), 21 * 60 + 5);
  assert.equal(M.parseClock('25:00'), null);
  assert.equal(M.parseClock('nada'), null);
});

test('itemLocal reads the Panamá wall clock off the string', () => {
  assert.deepEqual(M.itemLocal('2026-10-08T15:47:00-05:00'), { date: '2026-10-08', minutes: 947 });
  assert.equal(M.itemLocal('ayer'), null);
});

test('parseDayLabel: today, yesterday, weekdays and dates in English and Spanish', () => {
  const today = '2026-10-09'; // a Friday
  assert.equal(M.parseDayLabel('Today', today, LABELS), today);
  assert.equal(M.parseDayLabel('HOY', today, LABELS), today);
  assert.equal(M.parseDayLabel('Yesterday', today, LABELS), '2026-10-08');
  assert.equal(M.parseDayLabel('ayer', today, LABELS), '2026-10-08');
  assert.equal(M.parseDayLabel('Wednesday', today, LABELS), '2026-10-07');
  assert.equal(M.parseDayLabel('miércoles', today, LABELS), '2026-10-07');
  assert.equal(M.parseDayLabel('Friday', today, LABELS), '2026-10-02'); // a week ago
  assert.equal(M.parseDayLabel('10/3/2026', today, LABELS, 'mdy'), '2026-10-03');
  assert.equal(M.parseDayLabel('10/3/2026', today, LABELS, 'dmy'), '2026-03-10');
  assert.equal(M.parseDayLabel('25/9/2026', today, LABELS, 'mdy'), '2026-09-25');
  assert.equal(M.parseDayLabel('2026-09-25', today, LABELS), '2026-09-25');
  assert.equal(M.parseDayLabel('September 25, 2026', today, LABELS), '2026-09-25');
  assert.equal(M.parseDayLabel('25 de septiembre de 2026', today, LABELS), '2026-09-25');
  assert.equal(M.parseDayLabel('9:41 AM', today, LABELS), null);
  assert.equal(M.parseDayLabel('Hola', today, LABELS), null);
  assert.equal(M.parseDayLabel('2/30/2026', today, LABELS), null);
});

test('captionMatches: equal after normalizing, or a prefix when the API cut it', () => {
  assert.ok(M.captionMatches('receta  nueva', 'receta nueva'));
  assert.ok(M.captionMatches('', ''));
  assert.ok(!M.captionMatches('', 'algo'));
  const long = 'a'.repeat(250);
  assert.ok(M.captionMatches(long.slice(0, 200), long));
  assert.ok(!M.captionMatches('a'.repeat(50), long));
});

test('kindMatches maps stored kinds to bubble kinds', () => {
  assert.ok(M.kindMatches('pdf', 'document'));
  assert.ok(M.kindMatches('audio', 'audio'));
  assert.ok(M.kindMatches('other', 'video'));
  assert.ok(!M.kindMatches('image', 'document'));
  assert.ok(!M.kindMatches('image', null));
});

const ITEM = {
  id: 7,
  sent_at: '2026-10-08T15:47:00-05:00',
  sender: 'Beto Ficticio',
  media_kind: 'image',
  media_filename: null,
  caption: 'receta nueva',
  twin_index: 0,
  twin_count: 1,
};
const BUBBLE = {
  key: 'A'.repeat(32),
  date: '2026-10-08',
  minutes: 947,
  sender: 'Beto Ficticio',
  kind: 'image',
  caption: 'receta nueva',
  filename: null,
};

test('bubbleMatches needs day, minute, sender, kind and caption', () => {
  assert.ok(M.bubbleMatches(ITEM, BUBBLE));
  assert.ok(!M.bubbleMatches(ITEM, { ...BUBBLE, date: '2026-10-07' }));
  assert.ok(!M.bubbleMatches(ITEM, { ...BUBBLE, minutes: 948 }));
  assert.ok(!M.bubbleMatches(ITEM, { ...BUBBLE, sender: null }));
  assert.ok(!M.bubbleMatches(ITEM, { ...BUBBLE, kind: 'audio' }));
  assert.ok(!M.bubbleMatches(ITEM, { ...BUBBLE, caption: '' }));
  const doc = { ...ITEM, media_kind: 'pdf', media_filename: 'Lab Prueba.pdf', caption: '' };
  const docBubble = { ...BUBBLE, kind: 'document', caption: '', filename: 'Lab Prueba.pdf' };
  assert.ok(M.bubbleMatches(doc, docBubble));
  assert.ok(!M.bubbleMatches(doc, { ...docBubble, filename: 'Otro.pdf' }));
  assert.equal(M.filenameFromTitle('View "Lab Prueba.pdf"'), 'Lab Prueba.pdf');
});

test('pickCandidate never guesses between twins', () => {
  const a = { key: 'A' };
  const b = { key: 'B' };
  assert.deepEqual(M.pickCandidate(ITEM, []), { skip: 'not_found' });
  assert.deepEqual(M.pickCandidate(ITEM, [a]), { candidate: a });
  assert.deepEqual(M.pickCandidate(ITEM, [a, b]), { skip: 'ambiguous' });
  const twin = { ...ITEM, twin_index: 1, twin_count: 2 };
  assert.deepEqual(M.pickCandidate(twin, [a, b]), { candidate: b });
  assert.deepEqual(M.pickCandidate(twin, [a]), { skip: 'ambiguous' });
});

test('isWaKey accepts only the 32-hex data-id', () => {
  assert.ok(M.isWaKey('0123456789ABCDEF0123456789ABCDEF'));
  assert.ok(!M.isWaKey('0123456789abcdef0123456789abcdef'));
  assert.ok(!M.isWaKey('false_123@g.us_ABC'));
});

test('every selector entry documents itself and says whether it is verified', () => {
  for (const [name, spec] of Object.entries(SELECTORS)) {
    assert.equal(typeof spec.css, 'string', name);
    assert.ok(spec.description, name);
    assert.equal(typeof spec.verified, 'boolean', name);
  }
  for (const key of ['exportChat', 'download', 'exportConfirm', 'cancel', 'moreSubmenu']) {
    assert.ok(LABELS[key].some((re) => re.test('x') === false), key);
  }
  assert.ok(LABELS.download.some((re) => re.test('Descargar')));
  assert.ok(LABELS.exportChat.some((re) => re.test('Export chat')));
});

test('chooseBubble: mixed kinds in one minute never fill a twin count by accident', () => {
  // Server: Ana's photo and Ana's caption-less voice note at 9:41 are
  // different groups (kind is part of the twin key), each twin_count 1.
  const at = { date: '2026-10-08', minutes: 9 * 60 + 41, caption: '', filename: null };
  const photo = { ...at, key: 'P', sender: 'Ana Prueba', kind: 'image' };
  const anaVoice = { ...at, key: 'V', sender: 'Ana Prueba', kind: 'audio' };
  // The operator's own voice note, misattributed to Ana (outgoing marker unverified).
  const ownVoice = { ...at, key: 'O', sender: 'Ana Prueba', kind: 'audio' };
  const voiceItem = {
    sent_at: '2026-10-08T09:41:30-05:00',
    sender: 'Ana Prueba',
    media_kind: 'audio',
    caption: '',
    twin_index: 0,
    twin_count: 1,
  };
  const photoItem = { ...voiceItem, media_kind: 'image' };
  assert.deepEqual(M.chooseBubble(voiceItem, [photo, anaVoice, ownVoice]), { skip: 'ambiguous' });
  assert.deepEqual(M.chooseBubble(voiceItem, [photo, anaVoice]), { candidate: anaVoice });
  assert.deepEqual(M.chooseBubble(photoItem, [photo, anaVoice, ownVoice]), { candidate: photo });
  // A cross-kind count (the old server grouping, twin_count 2) never picks.
  const crossKind = { ...voiceItem, twin_index: 1, twin_count: 2 };
  assert.deepEqual(M.chooseBubble(crossKind, [photo, anaVoice]), { skip: 'ambiguous' });
});

test('chooseBubble skips when a row of the day is unreadable, and unknown kinds', () => {
  assert.deepEqual(M.chooseBubble(ITEM, [BUBBLE]), { candidate: BUBBLE });
  assert.deepEqual(M.chooseBubble(ITEM, [BUBBLE, { key: 'X', unreadable: true }]), {
    skip: 'unreadable',
  });
  assert.deepEqual(M.chooseBubble({ ...ITEM, media_kind: 'other' }, [BUBBLE]), {
    skip: 'other_kind',
  });
});

test('isCaptureFor accepts only the capture armed now', () => {
  const msg = { source: 'n16chat-capture', id: 'B', ok: true };
  assert.ok(M.isCaptureFor('B', msg));
  assert.ok(!M.isCaptureFor('C', msg)); // a late answer for an earlier capture
  assert.ok(!M.isCaptureFor(undefined, msg)); // nothing armed
  assert.ok(!M.isCaptureFor('B', { ...msg, source: 'page' }));
});

test('exportRange runs from the Panamá day of the newest stored message to today', () => {
  assert.deepEqual(M.exportRange('2026-10-08T19:55:00-05:00', '2026-10-09'), {
    from: '2026-10-08',
    to: '2026-10-09',
  });
  assert.deepEqual(M.exportRange('2026-10-09T08:00:00-05:00', '2026-10-09'), {
    from: '2026-10-09',
    to: '2026-10-09',
  });
  // A device clock behind the server never gives from > to.
  assert.deepEqual(M.exportRange('2026-10-10T01:00:00-05:00', '2026-10-09'), {
    from: '2026-10-09',
    to: '2026-10-09',
  });
  assert.equal(M.exportRange(null, '2026-10-09'), null);
  assert.equal(M.exportRange('2026-10-08T19:55:00-05:00', 'nope'), null);
});

test('export dialog selectors are the calibrated radio group and date inputs', () => {
  assert.match(SELECTORS.exportRangeAll.css, /value="all"/);
  assert.match(SELECTORS.exportRangeCustom.css, /value="custom"/);
  assert.equal(SELECTORS.exportDateInput.css, 'input[type="date"]');
});

test('export date inputs are told apart by aria-label, never by order', () => {
  const from = SELECTORS.exportDateFrom.text;
  const to = SELECTORS.exportDateTo.text;
  for (const label of ['Start date', 'From', 'Fecha de inicio', 'Desde']) {
    assert.ok(from.test(label), label);
    assert.ok(!to.test(label), label);
  }
  for (const label of ['End date', 'To', 'Fecha de finalización', 'Hasta']) {
    assert.ok(to.test(label), label);
    assert.ok(!from.test(label), label);
  }
  assert.equal(SELECTORS.exportDateFrom.css, SELECTORS.exportDateInput.css);
  assert.equal(SELECTORS.exportDateTo.css, SELECTORS.exportDateInput.css);
});

test('reportLabels keeps known UI labels only and counts the rest', () => {
  const report = M.reportLabels(
    ['Reply', 'Message Ana Prueba', 'Descargar', 'receta-ficticia.pdf', '', 'Reply', ' Cancel '],
    LABELS.known,
  );
  assert.deepEqual(report, { labels: ['Reply', 'Descargar', 'Cancel'], unknown_labels: 2 });
  // Every label the flow itself looks for is reportable.
  const flow = ['Export chat', 'Exportar chat', 'More', 'Download', 'Cancelar', 'Export', 'Start date', 'End date'];
  for (const label of flow) {
    assert.deepEqual(M.reportLabels([label], LABELS.known).labels, [label], label);
  }
  // Header buttons and chat names are not menu items.
  assert.deepEqual(M.reportLabels(['Grupo Familia Prueba', 'Menu'], LABELS.known).labels, []);
  assert.equal(M.reportLabels(['Reply', 'Copy', 'Star'], LABELS.known, 2).labels.length, 2);
});

test('pageBack tries the paging steps in order until the list grows', async () => {
  const steps = ['top', 'first_row', 'wheel'];
  const applied = [];
  const apply = async (step) => applied.push(step);
  const grows = (winner) => async (step) => step === winner;
  assert.equal(await M.pageBack(steps, apply, grows('top')), 'top');
  assert.deepEqual(applied, ['top']);
  applied.length = 0;
  assert.equal(await M.pageBack(steps, apply, grows('wheel')), 'wheel');
  assert.deepEqual(applied, steps);
  applied.length = 0;
  // Nothing grows: the beginning of the chat (every step tried once).
  assert.equal(await M.pageBack(steps, apply, grows(null)), null);
  assert.deepEqual(applied, steps);
});
