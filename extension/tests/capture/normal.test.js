import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

const page = await loadPage();

test('an armed capture gets the file under its own id; nothing reaches the disk', async () => {
  const first = page.D.armCapture(60_000);
  page.pageSaves('primero');
  const result = await first.done;
  assert.equal(result.ok, true);
  assert.equal(result.id, first.id);
  assert.equal(page.saved.length, 0);

  // One capture per arm: a later save is the page's own (not captured).
  page.pageSaves('normal');
  assert.equal(page.saved.length, 1);

  // A plain cancel (nothing clicked yet) does not quarantine.
  const cancelled = page.D.armCapture(60_000);
  cancelled.cancel();
  assert.equal((await cancelled.done).reason, 'cancelled');
  const next = page.D.armCapture(60_000);
  page.pageSaves('siguiente');
  const ok = await next.done;
  assert.equal(ok.ok, true);
  assert.equal(ok.id, next.id);
});
