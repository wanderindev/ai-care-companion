// MAIN-world script on web.whatsapp.com (document_start). Captures the file
// the page is about to save — the chat export or a media download — so the
// extension gets the bytes without touching the user's disk.
//
// It does nothing unless the extension's content script has *armed* it for
// one capture (window message {source: "n16chat-ext", type: "arm", id}).
// It acknowledges every arm it accepts with {source: "n16chat-capture", id,
// armed: true}; the content script clicks nothing that can save before
// that ack (dom.js `armed`): a window message is delivered in a later task,
// and WhatsApp saves a voice note it already holds in the same task as the
// «Download» click, so a click right after posting "arm" would reach the
// disk. No ack within 3 s → the content script sends a plain disarm and
// stops with `capture_not_armed` (nothing was clicked, so the page stays
// clean); a refused arm is answered {id, ok: false, reason: "quarantine"}
// instead (below). While armed, the first save the page starts is intercepted, kept from
// reaching the browser's download manager, and posted back as a Blob
// ({source: "n16chat-capture", id, ...}); the capture then disarms. Covered
// save paths (which one WhatsApp uses is a calibration item, README):
//   - an <a download href="blob:|data:"> clicked via .click(), dispatchEvent
//     or a real click;
//   - window.showSaveFilePicker (File System Access API).
// A download with an https URL is blocked too and reported as
// "remote_url": the extension cannot read it without the disk.
//
// Late saves: a capture that is not answered — it expires, the content
// script gives up on it ("late" disarm: a timeout, an abandoned capture),
// or a new arm arrives while one is still armed — may still be answered by
// the page later. The script then enters *quarantine* until the tab is
// reloaded (this state lives in the page and is lost on reload): every
// save is swallowed (neither delivered nor written to disk) and every arm
// is refused at once with reason "quarantine". The extension reloads the
// WhatsApp tab at the start of every sync, so each run starts clean and a
// file clicked in one capture can only ever be delivered under that
// capture's id.
//
// Privacy: never logs anything about the file.

(() => {
  if (window.__n16CaptureInstalled) return;
  window.__n16CaptureInstalled = true;

  const IN = 'n16chat-ext';
  const OUT = 'n16chat-capture';
  const MAX_BLOBS = 20;

  let armed = null; // { id, until }
  let quarantined = false; // until the tab is reloaded; never expires
  const blobs = new Map(); // object URL → Blob, recorded only while armed

  function quarantine() {
    armed = null;
    blobs.clear();
    quarantined = true;
  }

  function isArmed() {
    if (armed && Date.now() >= armed.until) quarantine(); // expired unanswered
    return Boolean(armed);
  }

  function post(payload) {
    window.postMessage({ source: OUT, ...payload }, location.origin);
  }

  function take() {
    const id = armed.id;
    armed = null;
    return id;
  }

  async function deliver(id, blob, name, via) {
    post({ id, ok: true, blob, name: name || '', type: blob.type || '', size: blob.size, via });
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.data?.source !== IN) return;
    if (event.data.type === 'arm' && typeof event.data.id === 'string') {
      // A capture still armed (or expired) was never answered: its file may
      // come later, so nothing more is captured in this page.
      if (armed || quarantined) {
        quarantine();
        post({ id: event.data.id, ok: false, reason: 'quarantine' });
        return;
      }
      armed = { id: event.data.id, until: Date.now() + (Number(event.data.ms) || 120_000) };
      blobs.clear();
      // The content script waits for this before clicking anything: a
      // window message is delivered later, and WhatsApp saves a voice note
      // it already holds in the same task as the «Download» click.
      post({ id: event.data.id, armed: true });
    } else if (event.data.type === 'disarm') {
      if (event.data.late) quarantine();
      else {
        armed = null;
        blobs.clear();
      }
    }
  });

  const createObjectURL = URL.createObjectURL;
  URL.createObjectURL = function (object) {
    const url = createObjectURL.call(URL, object);
    if (isArmed() && object instanceof Blob) {
      blobs.set(url, object);
      if (blobs.size > MAX_BLOBS) blobs.delete(blobs.keys().next().value);
    }
    return url;
  };

  /** True when the anchor's save was captured (the caller must not save). */
  function intercept(anchor) {
    if (!(anchor instanceof HTMLAnchorElement) || !anchor.hasAttribute('download')) return false;
    if (!isArmed()) return quarantined; // in quarantine: swallow, deliver nothing
    const href = anchor.href || '';
    const name = anchor.getAttribute('download') || '';
    const id = take();
    if (href.startsWith('blob:') || href.startsWith('data:')) {
      const known = blobs.get(href);
      const blob = known ? Promise.resolve(known) : fetch(href).then((r) => r.blob());
      blob
        .then((b) => deliver(id, b, name, 'anchor'))
        .catch(() => post({ id, ok: false, reason: 'blob_unreadable' }));
    } else {
      post({ id, ok: false, reason: 'remote_url' });
    }
    return true;
  }

  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (intercept(this)) return undefined;
    return click.call(this);
  };

  const dispatchEvent = EventTarget.prototype.dispatchEvent;
  EventTarget.prototype.dispatchEvent = function (event) {
    if (this instanceof HTMLAnchorElement && event?.type === 'click' && intercept(this)) {
      return false;
    }
    return dispatchEvent.call(this, event);
  };

  document.addEventListener(
    'click',
    (event) => {
      const anchor = event.target?.closest?.('a[download]');
      if (anchor && intercept(anchor)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true,
  );

  if (typeof window.showSaveFilePicker === 'function') {
    const showSaveFilePicker = window.showSaveFilePicker;
    window.showSaveFilePicker = function (options) {
      if (!isArmed()) {
        if (!quarantined) return showSaveFilePicker.call(window, options);
        return Promise.reject(new DOMException('Cancelado', 'AbortError')); // swallowed
      }
      const id = take();
      const name = options?.suggestedName || '';
      const parts = [];
      const writable = {
        async write(chunk) {
          const data = chunk && typeof chunk === 'object' && 'data' in chunk ? chunk.data : chunk;
          if (data != null) parts.push(data);
        },
        async close() {
          await deliver(id, new Blob(parts), name, 'save_picker');
        },
        async abort() {
          post({ id, ok: false, reason: 'aborted' });
        },
        async seek() {},
        async truncate() {},
      };
      return Promise.resolve({
        kind: 'file',
        name,
        async createWritable() {
          return writable;
        },
        async getFile() {
          return new File(parts, name);
        },
      });
    };
  }
})();
