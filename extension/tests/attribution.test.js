// #118: day and sender attribution, time reading and the not_found funnel
// (pure helpers in src/content/match.js). Synthetic data only.
import { test } from 'node:test';
import assert from 'node:assert/strict';

await import('../src/content/selectors.js');
await import('../src/content/match.js');
const { LABELS } = globalThis.N16;
const M = globalThis.N16.match;

const TODAY = '2026-10-10'; // a Saturday

test('parseDayLabel: written dates with a weekday, abbreviations and no year', () => {
  const cases = [
    ['Thursday, October 8, 2026', '2026-10-08'],
    ['Thursday, October 8', '2026-10-08'],
    ['jueves, 8 de octubre de 2026', '2026-10-08'],
    ['jueves 8 de octubre', '2026-10-08'],
    ['Oct 8, 2026', '2026-10-08'],
    ['8 oct. 2026', '2026-10-08'],
    ['Sept 30', '2026-09-30'],
    ['December 24', '2025-12-24'], // without a year: never in the future
    ['Thu, Oct 8, 2026', '2026-10-08'],
    ['October 8, 2026', '2026-10-08'],
    ['Today', TODAY],
    ['Yesterday', '2026-10-09'],
    ['10/8/2026', '2026-10-08'],
  ];
  for (const [label, expected] of cases) {
    assert.equal(M.parseDayLabel(label, TODAY, LABELS, 'mdy'), expected, label);
  }
});

test('parseDayLabel refuses a weekday that does not fit the date, and non-dates', () => {
  assert.equal(M.parseDayLabel('Friday, October 8, 2026', TODAY, LABELS), null);
  assert.equal(M.parseDayLabel('3 unread messages', TODAY, LABELS), null);
  assert.equal(M.parseDayLabel('Messages are end-to-end encrypted', TODAY, LABELS), null);
  assert.equal(M.parseDayLabel('9:41 AM', TODAY, LABELS), null);
});

test('labelShape keeps the structure only: no letter or digit survives', () => {
  assert.equal(M.labelShape('Thursday, October 8'), 'A, A 9');
  assert.equal(M.labelShape('8 de octubre de 2026'), '9 a a a 9');
  assert.equal(M.labelShape('10/8/2026'), '9/9/9');
  assert.doesNotMatch(M.labelShape('Ana María 12'), /[B-Zb-z0-8]/);
  assert.ok(M.dateLike('October 8'));
  assert.ok(!M.dateLike('Today'));
});

test('metaMinutes: the pre-plain time wins, else the last clock in the meta', () => {
  assert.equal(M.metaMinutes('0:42 9:41 AM'), 9 * 60 + 41); // a duration before the time
  assert.equal(M.metaMinutes('Edited 3:07 PM'), 15 * 60 + 7);
  assert.equal(M.metaMinutes('9:41 AM', 9 * 60 + 40), 9 * 60 + 40);
  assert.equal(M.metaMinutes(''), null);
});

test('parsePrePlain reads day, minute and sender', () => {
  assert.deepEqual(M.parsePrePlain('[9:41 AM, 10/8/2026] Persona Uno: ', TODAY, LABELS, 'mdy'), {
    date: '2026-10-08',
    ambiguous: true, // 10/8 reads two ways
    minutes: 9 * 60 + 41,
    sender: 'Persona Uno',
  });
  assert.deepEqual(M.parsePrePlain('[21:05, 28/9/2026] Persona Dos: ', TODAY, LABELS, 'dmy'), {
    date: '2026-09-28',
    ambiguous: false,
    minutes: 21 * 60 + 5,
    sender: 'Persona Dos',
  });
  assert.deepEqual(M.parsePrePlain('nada', TODAY, LABELS), {
    date: null,
    ambiguous: false,
    minutes: null,
    sender: null,
  });
});

const sep = (date, top = 0) => ({ sep: true, date, top });
const row = (anchor = null, top = 0) => ({ row: true, anchor, top });

test('orderDayEntries keeps DOM order when separators sit among the rows', () => {
  const entries = [row(), sep('2026-10-08'), row(), sep('2026-10-09'), row()];
  assert.equal(M.orderDayEntries(entries).mode, 'dom');
  assert.equal(M.orderDayEntries(entries).ordered, entries);
});

test('orderDayEntries falls back to layout for separators kept apart (an overlay)', () => {
  const r1 = row(null, 100);
  const r2 = row(null, 300);
  const s1 = sep('2026-10-08', 50);
  const s2 = sep('2026-10-09', 200);
  const { mode, ordered } = M.orderDayEntries([r1, r2, s1, s2]);
  assert.equal(mode, 'layout');
  assert.deepEqual(ordered, [s1, r1, s2, r2]);
});

test('assignDays: rows take the separator before them; rows before any take null', () => {
  const { days, conflicts } = M.assignDays([
    row(),
    sep('2026-10-08'),
    row(),
    row(),
    sep('2026-10-09'),
    row(),
  ]);
  assert.deepEqual(days, [null, '2026-10-08', '2026-10-08', '2026-10-09']);
  assert.equal(conflicts, 0);
});

// Ambiguous = read through the guessed date order (could be day/month swapped).
const amb = (anchor) => ({ row: true, anchor, ambiguous: true, top: 0 });
const ambSep = (date) => ({ sep: true, date, ambiguous: true, top: 0 });
const unparsed = () => ({ sep: true, date: null, unparsed: true, top: 0 });

test('assignDays never guesses: a table of anchor / separator cases', () => {
  const N = null;
  const cases = [
    // [name, entries, expected days, expected conflicts]
    ['leading stretch, one unambiguous anchor',
      [row(), row('2026-10-07'), sep('2026-10-08'), row()],
      ['2026-10-07', '2026-10-07', '2026-10-08'], 0],
    ['leading stretch, only an ambiguous anchor: no day',
      [row(), amb('2026-09-10'), sep('2026-10-10'), row()],
      [N, N, '2026-10-10'], 0],
    ['anchors agree with the separator',
      [sep('2026-10-09'), row(), row('2026-10-09'), amb('2026-10-09')],
      ['2026-10-09', '2026-10-09', '2026-10-09'], 0],
    ['a relative separator (Yesterday) beats an ambiguous anchor (9/10 vs 10/9)',
      [sep('2026-10-09'), row(), amb('2026-09-10'), row()],
      ['2026-10-09', '2026-10-09', '2026-10-09'], 0],
    ['an unambiguous anchor against the separator: whole stretch null',
      [sep('2026-10-09'), row(), row('2026-10-07'), row()],
      [N, N, N], 1],
    ['an ambiguous separator against an ambiguous anchor: null',
      [ambSep('2026-09-10'), row(), amb('2026-10-09')],
      [N, N], 1],
    ['an ambiguous separator against an unambiguous anchor: null',
      [ambSep('2026-09-10'), row(), row('2026-10-09')],
      [N, N], 1],
    ['anchors disagreeing among themselves: null',
      [sep('2026-10-08'), row('2026-10-08'), row(), row('2026-10-09')],
      [N, N, N], 1],
    ['an unparsed separator ends the day before it; the rows after it get no day',
      [sep('2026-10-08'), row(), unparsed(), row(), row('2026-10-09'), sep('2026-10-10'), row()],
      ['2026-10-08', N, N, '2026-10-10'], 0],
  ];
  for (const [name, entries, days, conflicts] of cases) {
    const r = M.assignDays(entries);
    assert.deepEqual(r.days, days, name);
    assert.equal(r.conflicts, conflicts, name);
  }
});

test('dateAmbiguous: only numeric dates that read two ways', () => {
  assert.ok(M.dateAmbiguous('9/10/2026'));
  assert.ok(M.dateAmbiguous('10.9.26'));
  assert.ok(!M.dateAmbiguous('10/10/2026')); // same both ways
  assert.ok(!M.dateAmbiguous('13/10/2026'));
  assert.ok(!M.dateAmbiguous('Today'));
  assert.ok(!M.dateAmbiguous('October 9, 2026'));
  assert.ok(!M.dateAmbiguous('2026-10-09'));
});

const info = (over = {}) => ({
  author: null,
  runStart: false,
  outgoing: false,
  unreadable: false,
  date: '2026-10-08',
  ...over,
});

test('propagateSenders fills a run forward and backward', () => {
  const senders = M.propagateSenders([
    info({ runStart: true }), // a media bubble without a label
    info({ author: 'Persona Uno' }), // a later text bubble (pre-plain)
    info(),
    info({ runStart: true, author: 'Persona Dos' }),
    info(),
  ]);
  assert.deepEqual(senders, [
    'Persona Uno',
    'Persona Uno',
    'Persona Uno',
    'Persona Dos',
    'Persona Dos',
  ]);
});

test('propagateSenders breaks runs at a day change, an unreadable row and a new label', () => {
  const senders = M.propagateSenders([
    info({ runStart: true, author: 'Persona Uno' }),
    info({ date: '2026-10-09' }), // next day: unknown
    info({ date: '2026-10-09', unreadable: true }),
    info({ date: '2026-10-09' }), // after an unreadable row, until a label
    info({ date: '2026-10-09', author: 'Persona Dos' }),
    info({ date: '2026-10-09' }),
  ]);
  assert.deepEqual(senders, [
    'Persona Uno',
    null,
    null,
    'Persona Dos', // same run as the labelled bubble after it
    'Persona Dos',
    'Persona Dos',
  ]);
});

test('propagateSenders stops at a row whose own label conflicts with the run', () => {
  // An own reply misread as part of Persona Uno's run, labelled with the
  // quoted person: nothing spreads; each row keeps only its own label.
  const senders = M.propagateSenders([
    info({ runStart: true }), // media, no label
    info({ author: 'Persona Uno' }),
    info(), // media, no label
    info({ author: 'Persona Dos' }), // conflicting label, no tail
    info(), // media, no label
    info({ runStart: true, author: 'Persona Tres' }), // a new run is clean again
    info(),
  ]);
  assert.deepEqual(senders, [
    null,
    'Persona Uno',
    null,
    'Persona Dos',
    null,
    'Persona Tres',
    'Persona Tres',
  ]);
  // A conflicted own run never teaches the operator's name.
  const own = M.propagateSenders([
    info({ outgoing: true, author: 'Yo Mismo' }),
    info({ author: 'Persona Uno' }),
    info({ outgoing: true }),
  ]);
  assert.deepEqual(own, ['Yo Mismo', 'Persona Uno', null]);
});

test('rowAuthor: the pre-plain sender first, else one author label', () => {
  assert.equal(M.rowAuthor('Persona Uno', ['Persona Dos']), 'Persona Uno');
  assert.equal(M.rowAuthor(null, [' Persona Dos ']), 'Persona Dos');
  assert.equal(M.rowAuthor(null, ['Persona Dos', 'Persona Dos']), 'Persona Dos');
  assert.equal(M.rowAuthor(null, ['Persona Dos', 'Persona Tres']), null);
  assert.equal(M.rowAuthor('', []), null);
});

test('propagateSenders names own runs from an own labelled row, or ownName', () => {
  const rows = [
    info({ outgoing: true, author: 'Yo Mismo' }), // own text row (pre-plain)
    info({ runStart: true, author: 'Persona Uno' }),
    info({ outgoing: true }), // own media bubble, no label
    info(),
  ];
  assert.deepEqual(M.propagateSenders(rows), ['Yo Mismo', 'Persona Uno', 'Yo Mismo', 'Yo Mismo']);
  assert.deepEqual(M.propagateSenders(rows.slice(2)), [null, null]);
  assert.deepEqual(M.propagateSenders(rows.slice(2), 'Yo Mismo'), ['Yo Mismo', 'Yo Mismo']);
});

const item = {
  id: 7,
  sent_at: '2026-10-08T09:41:00-05:00',
  sender: 'Persona Uno',
  caption: '',
  media_kind: 'image',
  media_filename: null,
  twin_count: 1,
  twin_index: 0,
};
const bubble = (over = {}) => ({
  key: 'K',
  date: '2026-10-08',
  minutes: 9 * 60 + 41,
  kind: 'image',
  caption: '',
  filename: null,
  sender: 'Persona Uno',
  ...over,
});

test('explainMiss reports the first funnel stage that reaches zero', () => {
  const cases = [
    [[], 'day_rows'],
    [[{ key: 'T', date: '2026-10-08', kind: null }], 'media_rows'],
    [[bubble({ kind: 'audio' })], 'kind'],
    [[bubble({ minutes: 9 * 60 + 42 })], 'minute'],
    [[bubble({ sender: null })], 'sender'],
    [[bubble({ caption: 'otro texto' })], 'caption'],
    [[bubble()], 'none'],
  ];
  for (const [bubbles, stage] of cases) {
    assert.equal(M.explainMiss(item, bubbles).stage, stage, stage);
  }
  const doc = { ...item, media_kind: 'pdf', media_filename: 'a.pdf' };
  const docBubble = bubble({ kind: 'document', filename: 'b.pdf' });
  assert.equal(M.explainMiss(doc, [docBubble]).stage, 'filename');
});

test('explainMiss counts near misses: ±1 minute, whole hours, sender unknown, other day', () => {
  const { n } = M.explainMiss(
    item,
    [
      bubble({ minutes: 9 * 60 + 40 }),
      bubble({ minutes: 14 * 60 + 41 }), // 5 h off: a time zone
      bubble({ sender: null }),
    ],
    [bubble({ date: '2026-09-10', key: 'X' })],
  );
  assert.equal(n.minute_pm1, 1);
  assert.equal(n.minute_tz, 1);
  assert.equal(n.sender_null, 1);
  assert.equal(n.other_day, 1);
  assert.equal(n.minute, 1);
  assert.equal(n.sender, 0);
});

test('explainMiss output holds a stage and numbers only', () => {
  const out = M.explainMiss({ ...item, caption: 'texto privado' }, [bubble({ caption: 'algo' })]);
  assert.ok([...M.MISS_STAGES, 'none'].includes(out.stage));
  for (const value of Object.values(out.n)) assert.equal(typeof value, 'number');
});

test('daysBetween counts calendar days', () => {
  assert.equal(M.daysBetween('2026-10-03', TODAY), 7);
  assert.equal(M.daysBetween(TODAY, TODAY), 0);
});
