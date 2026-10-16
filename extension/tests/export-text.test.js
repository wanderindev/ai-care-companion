import { test } from 'node:test';
import assert from 'node:assert/strict';

import { dateOrder, summarizeExport } from '../src/shared/export-text.js';

const NNBSP = ' ';
// Synthetic messages only.
const WEB = [
  `[10/8/26, 9:14:00${NNBSP}PM] Beto Ficticio: antes`,
  `[10/8/26, 9:13:59${NNBSP}PM] Beto Ficticio: un minuto antes`,
  `[10/8/26, 9:41:05${NNBSP}PM] Ana Prueba: Hola`,
  `[10/9/26, 8:00:10${NNBSP}AM] Ana Prueba: <voice message omitted>`,
  'segunda línea',
  `[10/9/26, 8:01:00${NNBSP}AM] Beto Ficticio: <image omitted> receta`,
  `[10/13/26, 7:00:00${NNBSP}AM] Ana Prueba: ok`,
].join('\n');

test('counts messages and media from the last stored minute on', () => {
  const s = summarizeExport(WEB, '2026-10-08T21:14:00-05:00');
  assert.equal(s.order, 'mdy');
  assert.equal(s.messages, 6);
  assert.equal(s.fromSince, 5); // 9:14 PM is the same minute; 9:13 PM is before
  assert.equal(s.mediaFromSince, 2);
  assert.equal(s.newest, '2026-10-13T07:00');
});

test('without a last date everything counts', () => {
  assert.equal(summarizeExport(WEB, null).fromSince, 6);
});

test('date order: an impossible month decides, else fewer backwards steps', () => {
  const h = (a, b) => ({ parts: [a, b, '26'], hour: 1, minute: 0, ampm: null });
  assert.equal(dateOrder([h('13', '1')]), 'dmy');
  assert.equal(dateOrder([h('1', '13')]), 'mdy');
  // 2/1, 3/1, 4/1 goes forward as d/m (Jan 2, 3, 4) and as m/d too
  // (Feb 1, Mar 1, Apr 1): a tie goes to d/m, like the server.
  assert.equal(dateOrder([h('2', '1'), h('3', '1'), h('4', '1')]), 'dmy');
  // 1/5, 2/3: as m/d Jan 5 → Feb 3 (forward); as d/m May 1 → Mar 2 (backwards).
  assert.equal(dateOrder([h('1', '5'), h('2', '3')]), 'mdy');
});

test('Android (dash) lines are counted too', () => {
  const s = summarizeExport(
    '25/2/24, 10:00 - Ana Prueba: <Media omitted>\n25/2/24, 10:01 - Ana Prueba: x',
    null,
  );
  assert.equal(s.order, 'dmy');
  assert.equal(s.messages, 2);
  assert.equal(s.mediaFromSince, 1);
});
