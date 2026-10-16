import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

const page = await loadPage();

test('re-arming while a capture is armed quarantines the page; the late file goes nowhere', async () => {
  const x = page.D.armCapture(60_000); // item X: clicked, its file not here yet
  const y = page.D.armCapture(60_000); // item Y (or the export) arms anyway
  assert.equal((await x.done).reason, 'abandoned');
  assert.equal((await y.done).reason, 'quarantine');

  page.pageSaves('archivo tardío de X');
  await page.tick();
  assert.equal(page.saved.length, 0, 'swallowed, not written to disk');
  assert.ok(
    page.posted.every((p) => !p.ok),
    'and delivered to no capture',
  );

  // Still quarantined for every later capture of this page.
  const z = page.D.armCapture(60_000);
  assert.equal((await z.done).reason, 'quarantine');
});
