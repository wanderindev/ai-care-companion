import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

// main.js waits with `armed(capture)` (dom.js) before any click that can
// save. Both failure paths must stop the run with the right status.
const page = await loadPage({ asyncMessages: true });

test('no ack in time: capture_not_armed, and the page is left clean', async () => {
  page.dropArms(true);
  const capture = page.D.armCapture(60_000, 50);
  await assert.rejects(page.D.armed(capture), (err) => err.code === 'capture_not_armed');
  assert.equal((await capture.done).reason, 'cancelled');
  assert.equal(capture.settled(), true);

  // Not quarantined: the next capture is acknowledged and gets its file.
  page.dropArms(false);
  await page.tick();
  const next = page.D.armCapture(60_000, 1000);
  await page.D.armed(next);
  page.pageSaves('archivo de prueba');
  const result = await next.done;
  assert.equal(result.ok, true);
  assert.equal(result.id, next.id);
  assert.equal(page.saved.length, 0, 'nothing reached the disk');
});

test('a quarantine refusal stops with capture_quarantine', async () => {
  // A capture abandoned while armed (a late disarm) quarantines the page.
  const stale = page.D.armCapture(60_000, 1000);
  await page.D.armed(stale);
  page.D.abandonCapture();
  await page.tick();

  const capture = page.D.armCapture(60_000, 1000);
  await assert.rejects(page.D.armed(capture), (err) => err.code === 'capture_quarantine');
  assert.equal((await capture.done).reason, 'quarantine');
});
