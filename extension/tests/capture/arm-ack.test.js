import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

// Browsers deliver window messages in a later task. WhatsApp saves a voice
// note it already holds in the same task as the «Download» click, so a
// click made right after posting "arm" would save to disk (seen live
// 2026-10-09). The content script must wait for the page's ack.
const page = await loadPage({ asyncMessages: true });

test('a save in the same task as the arm is not captured (the race)', async () => {
  const capture = page.D.armCapture(60_000);
  page.pageSaves('demasiado pronto');
  assert.equal(page.saved.length, 1); // reached the disk
  capture.cancel();
  await capture.done;
});

test('ready resolves once the page is armed; a save after it is captured', async () => {
  const capture = page.D.armCapture(60_000);
  assert.equal(await capture.ready, true);
  page.pageSaves('a tiempo');
  const result = await capture.done;
  assert.equal(result.ok, true);
  assert.equal(result.id, capture.id);
  assert.equal(page.saved.length, 1); // still only the earlier one
});
