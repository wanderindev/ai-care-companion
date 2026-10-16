import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isOwnPage } from '../src/shared/run-helpers.js';

const ID = 'abcdefghijklmnopabcdefghijklmnop';
const ORIGIN = `chrome-extension://${ID}`;

test('the options page in a tab is accepted', () => {
  const sender = { id: ID, tab: { id: 7 }, url: `${ORIGIN}/src/options/options.html` };
  assert.equal(isOwnPage(sender, ID, ORIGIN), true);
});

test('the popup is accepted', () => {
  assert.equal(isOwnPage({ id: ID, url: `${ORIGIN}/src/popup/popup.html` }, ID, ORIGIN), true);
});

test('content scripts on WhatsApp Web are refused', () => {
  const sender = { id: ID, tab: { id: 3 }, url: 'https://web.whatsapp.com/' };
  assert.equal(isOwnPage(sender, ID, ORIGIN), false);
});

test('other extensions and missing data are refused', () => {
  assert.equal(isOwnPage({ id: 'other', url: `${ORIGIN}/x.html` }, ID, ORIGIN), false);
  assert.equal(isOwnPage({ id: ID }, ID, ORIGIN), false);
  assert.equal(isOwnPage(undefined, ID, ORIGIN), false);
  assert.equal(isOwnPage({ id: ID, url: `${ORIGIN}.evil.com/x` }, ID, ORIGIN), false);
});
