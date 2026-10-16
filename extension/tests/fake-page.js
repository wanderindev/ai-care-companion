// A minimal fake web.whatsapp.com page for the capture tests: window
// messaging, anchors, a controllable clock, and the extension's capture
// scripts (MAIN-world capture-main.js + the content script's dom.js).
// One page per test file (node --test runs each file in its own process).

/** `asyncMessages`: deliver window messages in a later task, as browsers do
 * (the default delivers them at once, which hides arm/click races). */
export async function loadPage({ asyncMessages = false } = {}) {
  const listeners = [];
  const posted = []; // capture answers seen on the window
  const saved = []; // anchors that reached the "browser" (= written to disk)
  let offset = 0;
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + offset;

  globalThis.window = globalThis;
  globalThis.location = { origin: 'https://web.whatsapp.com' };
  globalThis.addEventListener = (type, fn) => {
    if (type === 'message') listeners.push(fn);
  };
  const control = { dropArms: false };
  globalThis.postMessage = (data) => {
    // `dropArms`: the page never sees an arm (a content script whose
    // MAIN-world partner is missing or stuck), so no ack comes.
    if (control.dropArms && data?.source === 'n16chat-ext' && data.type === 'arm') return;
    const deliver = () => {
      if (data.source === 'n16chat-capture') posted.push(data);
      for (const fn of listeners) fn({ source: globalThis, data });
    };
    if (asyncMessages) setTimeout(deliver, 0);
    else deliver();
  };
  globalThis.document = { addEventListener() {} };
  globalThis.HTMLAnchorElement = class extends EventTarget {
    constructor(href, download) {
      super();
      this.href = href;
      this.attrs = download == null ? {} : { download };
    }
    hasAttribute(name) {
      return name in this.attrs;
    }
    getAttribute(name) {
      return this.attrs[name] ?? null;
    }
    click() {
      saved.push(this);
    }
  };

  await import('../src/content/capture-main.js');
  await import('../src/content/selectors.js');
  await import('../src/content/match.js');
  await import('../src/content/dom.js');

  return {
    D: globalThis.N16.dom,
    posted,
    saved,
    /** Stop (true) or resume (false) delivering arm messages to the page. */
    dropArms(value) {
      control.dropArms = value;
    },
    advance(ms) {
      offset += ms;
    },
    /** The page saves a file the way WhatsApp Web would (blob anchor). */
    pageSaves(text) {
      const url = URL.createObjectURL(new Blob([text]));
      new HTMLAnchorElement(url, 'file.bin').click();
    },
    tick: () => new Promise((resolve) => setTimeout(resolve, 5)),
  };
}
