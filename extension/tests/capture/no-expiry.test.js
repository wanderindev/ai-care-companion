import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

const page = await loadPage();

test('a timed-out capture quarantines the page with no time limit', async () => {
  const x = page.D.armCapture(20); // times out: a "late" disarm
  assert.equal((await x.done).reason, 'timeout');

  page.advance(60 * 60 * 1000); // an hour later (any earlier expiry is past)
  const y = page.D.armCapture(60_000);
  assert.equal((await y.done).reason, 'quarantine');

  page.pageSaves('archivo de X, muy tarde');
  page.pageSaves('otro');
  await page.tick();
  assert.equal(page.saved.length, 0);
  assert.ok(page.posted.every((p) => !p.ok));
});
