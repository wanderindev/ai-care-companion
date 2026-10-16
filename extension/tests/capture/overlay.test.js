import { test } from 'node:test';
import assert from 'node:assert/strict';

import { loadPage } from '../fake-page.js';

// overlayOpen / dismissOverlay decide whether Escape is pressed. A message
// row's hover buttons («React», «Reply»…) match the loose menuItem selector
// and known labels; counting them would press Escape with no menu open,
// and WhatsApp Web then closes the chat.
const page = await loadPage();
const { SELECTORS: S } = globalThis.N16;

// A tiny fake DOM: each element lists the selector strings it matches.
class FakeEl {
  constructor({ matches = [], label = '', parent = null, shown = true }) {
    this.matches = matches;
    this.label = label;
    this.parent = parent;
    this.shown = shown;
    this.isConnected = true;
  }
  getAttribute(name) {
    return name === 'aria-label' ? this.label : null;
  }
  get textContent() {
    return this.label;
  }
  getBoundingClientRect() {
    return this.shown ? { width: 10, height: 10, top: 0, left: 0 } : { width: 0, height: 0 };
  }
  closest(css) {
    for (let el = this; el; el = el.parent) if (el.matches.includes(css)) return el;
    return null;
  }
}

let elements = [];
const escapes = [];
globalThis.KeyboardEvent = class extends Event {};
globalThis.document.body = { dispatchEvent: () => {} };
globalThis.document.activeElement = { dispatchEvent: (e) => escapes.push(e.type) };
globalThis.document.querySelectorAll = (css) => elements.filter((el) => el.matches.includes(css));

function messageRowWithHoverButton(label) {
  const scroller = new FakeEl({ matches: [S.messagesScroller.css] });
  const row = new FakeEl({ matches: [S.messageRow.css], parent: scroller });
  const button = new FakeEl({ matches: [S.menuItem.css], label, parent: row });
  return [scroller, row, button];
}

test('a hover button with a known label inside a message row is not an open menu', () => {
  elements = [...messageRowWithHoverButton('React'), ...messageRowWithHoverButton('Reply')];
  assert.equal(page.D.overlayOpen(), false);
  assert.equal(page.D.dismissOverlay(), false);
  assert.deepEqual(escapes, [], 'no Escape: it would close the chat');
});

test('a real menu (outside the message list) still counts and is dismissed', () => {
  const menu = new FakeEl({ matches: ['menu'] });
  elements = [
    ...messageRowWithHoverButton('React'),
    new FakeEl({ matches: [S.menuItem.css], label: 'Download', parent: menu }),
    new FakeEl({ matches: [S.menuItem.css], label: 'Forward', parent: menu }),
  ];
  assert.equal(page.D.overlayOpen(), true);
  assert.equal(page.D.dismissOverlay(), true);
  assert.deepEqual(escapes, ['keydown']);
});

test('unknown labels, hidden items and an open dialog', () => {
  escapes.length = 0;
  elements = [new FakeEl({ matches: [S.menuItem.css], label: 'Grupo Familia Prueba' })];
  assert.equal(page.D.overlayOpen(), false);
  elements = [new FakeEl({ matches: [S.menuItem.css], label: 'Download', shown: false })];
  assert.equal(page.D.overlayOpen(), false);
  elements = [new FakeEl({ matches: [S.dialog.css] })];
  assert.equal(page.D.overlayOpen(), true);
});

test('contextMenu refuses a detached element', () => {
  const gone = new FakeEl({ matches: [S.bubble.css] });
  gone.isConnected = false;
  assert.throws(() => page.D.contextMenu(gone), (err) => err.code === 'element_gone');
});
