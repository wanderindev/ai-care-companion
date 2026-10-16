import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

const page = await loadPage();

test('a capture that expires inside the page (no word from the content script) quarantines', async () => {
  // Armed directly, as if the content script had died after arming.
  window.postMessage({ source: 'n16chat-ext', type: 'arm', id: 'X', ms: 1000 });
  page.advance(2000);
  page.pageSaves('archivo de X tras el plazo');
  await page.tick();
  assert.equal(page.saved.length, 0, 'swallowed');
  assert.ok(page.posted.every((p) => !p.ok), 'not delivered as X either');

  page.advance(30 * 60 * 1000);
  const next = page.D.armCapture(60_000);
  assert.equal((await next.done).reason, 'quarantine');
});
