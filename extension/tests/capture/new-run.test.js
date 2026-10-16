import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

const page = await loadPage();

test('a new run with a capture still pending (worker killed) quarantines the page', async () => {
  const x = page.D.armCapture(60_000); // the worker died while waiting on X
  page.D.abandonCapture(); // what n16:beginScan does for the next run
  assert.equal((await x.done).reason, 'abandoned');

  const exportCapture = page.D.armCapture(60_000);
  page.pageSaves('archivo de X');
  assert.equal((await exportCapture.done).reason, 'quarantine');
  await page.tick();
  assert.equal(page.saved.length, 0);
  assert.ok(page.posted.every((p) => !p.ok), 'X never becomes the export');
});
