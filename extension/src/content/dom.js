// DOM primitives for the WhatsApp Web content script: finding registry
// entries, waiting, safe clicks, row hydration and the capture handshake
// with capture-main.js. Never logs message content.

(() => {
  const N16 = (globalThis.N16 ??= {});
  const S = N16.SELECTORS;
  const L = N16.LABELS;
  const { normalizeText } = N16.match;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const ARM_ACK_MS = 3000;

  /** A stop with a Spanish status for the operator (code for the report). */
  class Stop extends Error {
    constructor(code, message, detail) {
      super(message);
      this.code = code;
      this.detail = detail;
    }
  }

  function labelOf(el) {
    return normalizeText(
      el.getAttribute?.('aria-label') || el.getAttribute?.('title') || el.textContent || '',
    );
  }

  function visible(el) {
    if (!el?.isConnected) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function all(spec, root = document) {
    const found = [...root.querySelectorAll(spec.css)];
    return spec.text ? found.filter((el) => spec.text.test(labelOf(el))) : found;
  }

  function one(spec, root = document) {
    return all(spec, root)[0] ?? null;
  }

  async function until(fn, { timeout = 10_000, every = 250 } = {}) {
    const end = Date.now() + timeout;
    for (;;) {
      const value = await fn();
      if (value) return value;
      if (Date.now() > end) return null;
      await sleep(every);
    }
  }

  /** Elements under `root` matching `spec` whose label matches one of the
   * `labels` regexes exactly. */
  function byLabel(spec, labels, root = document) {
    return all(spec, root).filter(
      (el) => visible(el) && labels.some((re) => re.test(labelOf(el))),
    );
  }

  function insideComposer(el) {
    return Boolean(el.closest(S.composeBox.css)) || Boolean(el.closest(S.composeInput.css));
  }

  /** Click like a user (pointer + mouse sequence). Refuses the composer. */
  function pointerClick(el) {
    if (!el?.isConnected) throw new Stop('element_gone', 'Un elemento de WhatsApp desapareció.');
    if (insideComposer(el)) {
      throw new Stop('composer_guard', 'Se evitó un clic en el cuadro de mensaje.');
    }
    const rect = el.getBoundingClientRect();
    const opts = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
      button: 0,
    };
    el.dispatchEvent(new PointerEvent('pointerdown', { ...opts, pointerType: 'mouse' }));
    el.dispatchEvent(new MouseEvent('mousedown', opts));
    el.dispatchEvent(new PointerEvent('pointerup', { ...opts, pointerType: 'mouse' }));
    el.dispatchEvent(new MouseEvent('mouseup', opts));
    el.dispatchEvent(new MouseEvent('click', opts));
  }

  function contextMenu(el) {
    if (!el?.isConnected) throw new Stop('element_gone', 'Un elemento de WhatsApp desapareció.');
    if (insideComposer(el)) {
      throw new Stop('composer_guard', 'Se evitó un clic en el cuadro de mensaje.');
    }
    const rect = el.getBoundingClientRect();
    el.dispatchEvent(
      new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        composed: true,
        view: window,
        clientX: rect.left + rect.width / 2,
        clientY: rect.top + rect.height / 2,
        button: 2,
      }),
    );
  }

  function pressEscape() {
    const opts = { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true, cancelable: true };
    (document.activeElement ?? document.body).dispatchEvent(new KeyboardEvent('keydown', opts));
    document.body.dispatchEvent(new KeyboardEvent('keyup', opts));
  }

  /** Inside the message list: a row's hover buttons («React», «Reply»…)
   * match the loose menuItem selector and known labels, but are no menu. */
  function insideMessages(el) {
    return Boolean(el.closest(S.messagesScroller.css)) || Boolean(el.closest(S.messageRow.css));
  }

  /** A WhatsApp menu or dialog is open: a visible dialog, or a visible menu
   * item with a known label (N16.LABELS.known) outside the message list.
   * Unknown elements matching the loose menuItem selector do not count. */
  function overlayOpen() {
    if (all(S.dialog).some(visible)) return true;
    return all(S.menuItem).some(
      (el) =>
        visible(el) &&
        !insideComposer(el) &&
        !insideMessages(el) &&
        L.known.some((re) => re.test(labelOf(el))),
    );
  }

  /** Escape only while a menu or dialog is open: with nothing open, Escape
   * makes WhatsApp Web close the chat. Returns whether it was pressed. */
  function dismissOverlay() {
    if (!overlayOpen()) return false;
    pressEscape();
    return true;
  }

  /** Put text in a plain <input> (the chat search) the way React notices. */
  function setInputValue(input, value) {
    if (!(input instanceof HTMLInputElement) || insideComposer(input)) {
      throw new Stop('composer_guard', 'El campo de búsqueda no es el esperado.');
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    input.focus();
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /** Set a native date input (export dialog) the way React notices. */
  function setDateValue(input, value) {
    if (!(input instanceof HTMLInputElement) || input.type !== 'date' || insideComposer(input)) {
      throw new Stop('date_guard', 'El campo de fecha no es el esperado.');
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return input.value === value;
  }

  // ── Rows ──

  function scroller() {
    return one(S.messagesScroller);
  }

  function rows() {
    const root = scroller();
    return root ? all(S.messageRow, root) : [];
  }

  function rowKey(row) {
    return one(S.messageKey, row)?.getAttribute('data-id') ?? null;
  }

  function hydrated(row) {
    return !one(S.virtualizedPlaceholder, row);
  }

  /** Scroll a row into view and wait for its content (recon §5). */
  async function hydrate(row) {
    if (hydrated(row)) return true;
    for (let attempt = 0; attempt < 2; attempt++) {
      row.scrollIntoView({ block: 'center' });
      if (await until(() => hydrated(row), { timeout: 4000 })) return true;
    }
    return false;
  }

  // ── Capture (see capture-main.js) ──

  const captures = new Map(); // id → Blob
  let pending = null; // { id, resolve }

  window.addEventListener('message', (event) => {
    const data = event.data;
    if (event.source !== window || data?.source !== 'n16chat-capture') return;
    if (data.armed === true) {
      if (pending && pending.id === data.id) pending.onArmed(true);
      return;
    }
    // Only the capture armed right now: a late answer for an earlier id
    // (timed out or cancelled) is dropped, never given to a later item.
    if (!N16.match.isCaptureFor(pending?.id, data)) return;
    const { resolve } = pending;
    pending = null;
    if (data.ok && data.blob instanceof Blob) {
      captures.set(data.id, data.blob);
      resolve({ ok: true, id: data.id, size: data.blob.size, type: data.type, name: data.name, via: data.via });
    } else {
      resolve({ ok: false, reason: data.reason || 'unknown' });
    }
  });

  /** Give up on a capture still pending (a new run, a new arm): the page
   * may still answer it, so the MAIN-world script is quarantined. */
  function abandonCapture() {
    if (!pending) return;
    const { resolve } = pending;
    pending = null;
    window.postMessage({ source: 'n16chat-ext', type: 'disarm', late: true }, location.origin);
    resolve({ ok: false, reason: 'abandoned' });
  }

  /** Arm the MAIN-world capture for one save; returns a waiter. `ackMs`:
   * how long to wait for the page's "armed" answer (tests shorten it). */
  function armCapture(ms = 120_000, ackMs = ARM_ACK_MS) {
    abandonCapture();
    const id = crypto.randomUUID();
    let resolve;
    const result = new Promise((r) => (resolve = r));
    let onArmed;
    const ready = new Promise((r) => (onArmed = r));
    // Any answer (e.g. a quarantine refusal) or no ack in time = not armed.
    result.then(() => onArmed(false));
    const ackTimer = setTimeout(() => onArmed(false), ackMs);
    ready.then(() => clearTimeout(ackTimer));
    pending = { id, resolve, onArmed };
    window.postMessage({ source: 'n16chat-ext', type: 'arm', id, ms }, location.origin);
    const timer = setTimeout(() => {
      if (pending?.id === id) {
        pending = null;
        // "late": the page may still save; capture-main.js quarantines it.
        window.postMessage({ source: 'n16chat-ext', type: 'disarm', late: true }, location.origin);
        resolve({ ok: false, reason: 'timeout' });
      }
    }, ms);
    return {
      id,
      /** Resolves true once the page confirms it is armed; click nothing
       * that can save before this (false: refused, or no answer). */
      ready,
      done: result.finally(() => clearTimeout(timer)),
      settled: () => pending?.id !== id,
      /** `late`: a click that may still save was already made. */
      cancel({ late = false } = {}) {
        if (pending?.id === id) {
          pending = null;
          window.postMessage({ source: 'n16chat-ext', type: 'disarm', late }, location.origin);
          resolve({ ok: false, reason: 'cancelled' });
        }
      },
    };
  }

  function quarantineStop() {
    return new Stop(
      'capture_quarantine',
      'Una descarga anterior de WhatsApp Web no terminó; vuelve a sincronizar (la extensión recarga la pestaña de WhatsApp Web primero).',
    );
  }

  /** Wait until the page confirms the capture is armed (main.js calls this
   * before any click that can save). A quarantine refusal stops the run
   * with `capture_quarantine`; no answer within the ack time cancels the
   * capture (a plain disarm: nothing was clicked, so the page is left
   * clean, not quarantined) and stops with `capture_not_armed`. */
  async function armed(capture) {
    if (await capture.ready) return;
    const result = capture.settled() ? await capture.done : null;
    if (result && !result.ok && result.reason === 'quarantine') throw quarantineStop();
    capture.cancel();
    throw new Stop(
      'capture_not_armed',
      'La captura de archivos no respondió en WhatsApp Web; recarga la pestaña y vuelve a sincronizar.',
    );
  }

  async function readCapture(id, offset, length) {
    const blob = captures.get(id);
    if (!blob) return null;
    const slice = new Uint8Array(await blob.slice(offset, offset + length).arrayBuffer());
    let binary = '';
    for (let i = 0; i < slice.length; i += 0x8000) {
      binary += String.fromCharCode(...slice.subarray(i, i + 0x8000));
    }
    return { base64: btoa(binary), size: blob.size };
  }

  function dropCapture(id) {
    if (id) captures.delete(id);
    else captures.clear();
  }

  N16.dom = {
    Stop,
    sleep,
    until,
    all,
    one,
    byLabel,
    labelOf,
    visible,
    pointerClick,
    contextMenu,
    pressEscape,
    overlayOpen,
    dismissOverlay,
    setInputValue,
    setDateValue,
    insideComposer,
    insideMessages,
    scroller,
    rows,
    rowKey,
    hydrated,
    hydrate,
    armCapture,
    armed,
    quarantineStop,
    abandonCapture,
    readCapture,
    dropCapture,
  };
})();
