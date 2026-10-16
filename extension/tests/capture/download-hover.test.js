import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

// #112: a media row's own hover «Download» button matches the loose
// menuItem selector and the «Descargar» labels. If the bubble's context
// menu does not open, that button (possibly another row's, under the
// mouse) must not be clicked while the capture is armed for this bubble:
// it would attach another message's file to this one.
const page = await loadPage();
const { SELECTORS: S } = globalThis.N16;

const GROUP = 'Grupo Familia Prueba';
const KEY = 'a'.repeat(32);

let elements = [];
const clicks = []; // labels of the elements that got a click
let onContextMenu = () => {};

class FakeEl {
  constructor({ matches = [], label = '', parent = null, attrs = {} }) {
    this.matches = matches;
    this.label = label;
    this.parent = parent;
    this.attrs = attrs;
    this.isConnected = true;
    elements.push(this);
  }
  getAttribute(name) {
    if (name === 'aria-label') return this.label || null;
    return this.attrs[name] ?? null;
  }
  get textContent() {
    return this.label;
  }
  getBoundingClientRect() {
    return { width: 10, height: 10, top: 0, left: 0 };
  }
  closest(css) {
    for (let el = this; el; el = el.parent) if (el.matches.includes(css)) return el;
    return null;
  }
  contains(other) {
    for (let el = other; el; el = el.parent) if (el === this) return true;
    return false;
  }
  querySelectorAll(css) {
    return elements.filter((el) => el !== this && el.matches.includes(css) && this.contains(el));
  }
  scrollIntoView() {}
  dispatchEvent(event) {
    if (event.type === 'click') clicks.push(this.label);
    if (event.type === 'contextmenu') onContextMenu(this);
    return true;
  }
}

globalThis.MouseEvent = class extends Event {};
globalThis.PointerEvent = class extends Event {};
globalThis.KeyboardEvent = class extends Event {};
globalThis.document.body = { dispatchEvent: () => {} };
globalThis.document.activeElement = { dispatchEvent: () => {} };
globalThis.document.querySelectorAll = (css) => elements.filter((el) => el.matches.includes(css));

let listener;
globalThis.chrome = { runtime: { onMessage: { addListener: (fn) => (listener = fn) } } };
await import('../../src/content/main.js');

function send(message) {
  return new Promise((resolve) => listener(message, {}, resolve));
}

/** The open group with two media rows; each row shows a hover «Download». */
function chatWithMediaRows() {
  elements = [];
  clicks.length = 0;
  new FakeEl({ matches: [S.headerTitle.css], label: GROUP });
  const scroller = new FakeEl({ matches: [S.messagesScroller.css] });
  for (const [i, key] of [KEY, 'b'.repeat(32)].entries()) {
    const row = new FakeEl({ matches: [S.messageRow.css], parent: scroller });
    const keyEl = new FakeEl({ matches: [S.messageKey.css], parent: row, attrs: { 'data-id': key } });
    const bubble = new FakeEl({ matches: [S.bubble.css], label: `bubble ${i}`, parent: keyEl });
    new FakeEl({ matches: [S.menuItem.css], label: 'Download', parent: bubble });
  }
}

test('no context menu + a hover «Download» in a row → skip, nothing clicked', async () => {
  chatWithMediaRows();
  // The context menu never opens: once the menu wait has started, move the
  // clock past it.
  onContextMenu = () => setTimeout(() => page.advance(10_000), 50);
  const result = await send({ type: 'n16:download', group: GROUP, key: KEY });
  assert.equal(result.ok, true);
  assert.equal(result.skip, 'download_failed');
  assert.equal(result.failure, 'download_item_missing');
  assert.equal(result.detail.found, 0);
  assert.deepEqual(clicks, [], 'a row hover button must never be clicked');
  assert.equal(page.saved.length, 0);
});

test('the context menu outside the list is used, not the hover buttons', async () => {
  chatWithMediaRows();
  onContextMenu = () => {
    const menu = new FakeEl({ matches: ['menu'] });
    const item = new FakeEl({ matches: [S.menuItem.css], label: 'Descargar', parent: menu });
    item.dispatchEvent = (event) => {
      if (event.type === 'click') {
        clicks.push('menu');
        page.pageSaves('archivo');
      }
      return true;
    };
  };
  const result = await send({ type: 'n16:download', group: GROUP, key: KEY });
  assert.equal(result.ok, true);
  assert.equal(result.capture?.ok, true);
  assert.deepEqual(clicks, ['menu']);
});
