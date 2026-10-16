// WhatsApp Web content script: short, bounded commands from the service
// worker (select the group, export the chat text, locate a media bubble,
// download it into a capture, hand the captured bytes over). Each command
// either does exactly what it expects or stops with a Spanish status; it
// never clicks something it did not identify, never types into the
// composer and never sends anything. Message content (senders, captions,
// file names) stays in this tab's memory: it is never logged and never
// returned to the worker except as match results (keys and counts).

(() => {
  if (window.__n16SyncLoaded) return;
  window.__n16SyncLoaded = true;

  const N16 = globalThis.N16;
  const S = N16.SELECTORS;
  const L = N16.LABELS;
  const M = N16.match;
  const D = N16.dom;
  const { Stop } = D;

  const MENU_WAIT = 4000;

  // Per sync run: bubbles already read (data-id → info). Reset by beginScan.
  let cache = new Map();
  let dateOrderSetting = 'auto';

  /** UI labels for a stop report (kept in the run state and shown in the
   * popup): only labels from LABELS.known, the others only counted. */
  function labelReport(elements) {
    return M.reportLabels(
      elements.map((el) => D.labelOf(el)),
      L.known,
    );
  }

  function visibleMenuLabels() {
    return labelReport(D.all(S.menuItem).filter((el) => D.visible(el) && !outsideMenu(el)));
  }

  /** Not part of an open menu: the composer, or the message list, whose
   * rows have hover buttons («Download», «React»…) that match the loose
   * menuItem selector. WhatsApp renders menus outside the list (#112). */
  function outsideMenu(el) {
    return D.insideComposer(el) || D.insideMessages(el);
  }

  function today() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  }

  /** "mdy" / "dmy" for numeric day separators: the setting, else a text
   * message's data-pre-plain-text date, else the UI language. */
  function dateOrder() {
    if (dateOrderSetting === 'mdy' || dateOrderSetting === 'dmy') return dateOrderSetting;
    for (const el of D.all(S.prePlainText)) {
      const m = (el.getAttribute('data-pre-plain-text') || '').match(/(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}\]/);
      if (m && Number(m[1]) > 12) return 'dmy';
      if (m && Number(m[2]) > 12) return 'mdy';
    }
    const lang = (document.documentElement.lang || '').toLowerCase();
    return lang === 'en' || lang === 'en-us' ? 'mdy' : 'dmy';
  }

  // ── App and chat ──

  async function waitForApp() {
    const root = await D.until(() => D.one(S.appRoot), { timeout: 60_000, every: 1000 });
    if (!root) {
      throw new Stop(
        'not_ready',
        'WhatsApp Web no está listo: abre la pestaña e inicia sesión (código QR) y vuelve a intentar.',
      );
    }
  }

  function openChatTitle() {
    const header = D.one(S.headerTitle);
    return header ? M.normalizeText(header.textContent) : null;
  }

  function chatRowsTitled(group) {
    const wanted = M.normalizeText(group);
    return D.all(S.chatRowTitle).filter(
      (el) => D.visible(el) && M.normalizeText(el.getAttribute('title')) === wanted,
    );
  }

  async function openRow(titleEl) {
    const row = titleEl.closest(S.chatListRow.css) ?? titleEl;
    D.pointerClick(D.one(S.chatRowClickable, row) ?? titleEl);
  }

  async function selectChat({ group }) {
    if (!group || !M.normalizeText(group)) {
      throw new Stop('no_group', 'Falta el nombre del grupo en las opciones de la extensión.');
    }
    await waitForApp();
    const wanted = M.normalizeText(group);
    if (openChatTitle() !== wanted) {
      let matches = chatRowsTitled(group);
      if (matches.length === 0) {
        const search = D.one(S.chatSearch);
        if (!search) throw new Stop('search_missing', 'No encontré el buscador de chats de WhatsApp.');
        D.setInputValue(search, group);
        await D.until(() => chatRowsTitled(group).length > 0, { timeout: 8000 });
        await D.sleep(500); // let the filtered list settle
        matches = chatRowsTitled(group);
      }
      if (matches.length === 0) {
        throw new Stop('group_not_found', 'No encontré el grupo configurado en la lista de chats.');
      }
      if (matches.length > 1) {
        throw new Stop('group_ambiguous', 'Hay más de un chat con el nombre del grupo; no elijo al azar.');
      }
      await openRow(matches[0]);
      const opened = await D.until(() => openChatTitle() === wanted, { timeout: 8000 });
      if (!opened) throw new Stop('wrong_chat', 'No se pudo abrir el grupo configurado.');
    }
    const scroller = await D.until(() => D.scroller(), { timeout: 10_000 });
    if (!scroller) throw new Stop('no_messages', 'No encontré la lista de mensajes del grupo.');
    return { ok: true };
  }

  async function ensureChat(group) {
    if (openChatTitle() !== M.normalizeText(group)) {
      throw new Stop('wrong_chat', 'El grupo configurado ya no está abierto en WhatsApp Web.');
    }
  }

  // ── Menus ──

  function menuItems(labels) {
    // A row's hover «Download» must never stand in for the context menu's
    // item: it would save another row's file into this bubble's capture.
    return D.byLabel(S.menuItem, labels).filter((el) => !outsideMenu(el));
  }

  async function openHeaderMenu() {
    const buttons = D.all(S.headerMenuButton).filter(D.visible);
    if (buttons.length !== 1) {
      throw new Stop('menu_missing', 'No encontré el botón «Menú» del chat.', { found: buttons.length });
    }
    D.pointerClick(buttons[0]);
  }

  function openDialog() {
    return D.all(S.dialog).find(D.visible) ?? null;
  }

  // Capture handshake (dom.js, tested in tests/capture/armed.test.js).
  const { armed, quarantineStop } = D;

  /** Set the export dialog's range (custom from `range.from` to `range.to`,
   * or «All messages» when `range` is null) and click its one confirm
   * button. Throws a Stop on any other dialog shape; the caller cancels. */
  async function fillExportDialog(dialog, buttons, range) {
    const fieldsStop = (detail) =>
      new Stop(
        'export_dialog_fields',
        'El diálogo de «Exportar chat» no es el esperado (rango de fechas); no exporté nada. Hay que calibrarla.',
        { ...labelReport(buttons), ...detail },
      );
    const fields = () =>
      dialog.querySelectorAll(
        'input, select, textarea, [role="checkbox"], [role="radio"], [role="combobox"]',
      );
    const all = D.one(S.exportRangeAll, dialog);
    const custom = D.one(S.exportRangeCustom, dialog);
    const extra = [...fields()].filter((el) => el !== all && el !== custom);
    if (fields().length && (!all || !custom || extra.length)) {
      throw fieldsStop({ fields: fields().length });
    }
    if (all && custom) {
      if (range) {
        D.pointerClick(custom.closest('label') ?? custom);
        // From / To by their aria-labels, never by DOM order.
        const dates = await D.until(
          () => {
            if (D.all(S.exportDateInput, dialog).length !== 2) return null;
            const from = D.all(S.exportDateFrom, dialog);
            const to = D.all(S.exportDateTo, dialog);
            return from.length === 1 && to.length === 1 && from[0] !== to[0]
              ? { from: from[0], to: to[0] }
              : null;
          },
          { timeout: 5000 },
        );
        if (!dates || !custom.checked) {
          throw fieldsStop({
            step: 'custom',
            dates: labelReport(D.all(S.exportDateInput, dialog)),
          });
        }
        const ok = D.setDateValue(dates.from, range.from) && D.setDateValue(dates.to, range.to);
        await D.sleep(300);
        if (!ok || dates.from.value !== range.from || dates.to.value !== range.to) {
          throw fieldsStop({ step: 'dates' });
        }
      } else if (!all.checked) {
        throw fieldsStop({ step: 'all' });
      }
    }
    const confirm = buttons.filter((b) => L.exportConfirm.some((re) => re.test(D.labelOf(b))));
    if (confirm.length !== 1) {
      throw new Stop(
        'export_dialog_unknown',
        'El diálogo de «Exportar chat» no es el esperado; no hice clic en nada.',
        labelReport(buttons),
      );
    }
    D.pointerClick(confirm[0]);
  }

  // ── Export (text only: WhatsApp Web has no media option) ──

  async function exportChat({ group, lastMessageAt }) {
    await ensureChat(group);
    await openHeaderMenu();
    let item = await D.until(() => menuItems(L.exportChat)[0], { timeout: MENU_WAIT });
    if (!item) {
      const more = menuItems(L.moreSubmenu);
      if (more.length === 1) {
        D.pointerClick(more[0]);
        item = await D.until(() => menuItems(L.exportChat)[0], { timeout: MENU_WAIT });
      }
    }
    if (!item) {
      const report = visibleMenuLabels();
      D.dismissOverlay();
      throw new Stop('export_item_missing', 'No encontré «Exportar chat» en el menú del chat.', report);
    }

    const capture = D.armCapture(180_000);
    try {
      await armed(capture);
    } catch (err) {
      D.dismissOverlay(); // the chat menu is still open
      throw err;
    }
    D.pointerClick(item);
    const first = await D.until(
      () => (capture.settled() ? 'capture' : openDialog() ? 'dialog' : null),
      { timeout: 15_000 },
    );
    if (first === 'dialog') {
      const dialog = openDialog();
      const buttons = D.all(S.dialogButton, dialog).filter(D.visible);
      // Close the dialog without exporting: its «Cancelar», else Escape
      // (only while it is still open, so the chat is never closed).
      const cancelDialog = () => {
        const button = buttons.find(
          (b) => b.isConnected && L.cancel.some((re) => re.test(D.labelOf(b))),
        );
        try {
          if (button) {
            D.pointerClick(button);
            return;
          }
        } catch {
          // fall through to Escape
        }
        D.dismissOverlay();
      };
      // Every way out of the dialog before the confirm click — a stop of
      // ours, a vanished element (`element_gone`), an unexpected date field
      // (`date_guard`), a script error — cancels the capture (a plain
      // disarm: nothing that saves was clicked) and the dialog.
      try {
        await fillExportDialog(dialog, buttons, M.exportRange(lastMessageAt, today()));
      } catch (err) {
        capture.cancel();
        cancelDialog();
        if (!(err instanceof Stop) || err.code.startsWith('export_dialog')) throw err;
        throw new Stop(
          'export_dialog_fields',
          'El diálogo de «Exportar chat» no es el esperado (rango de fechas); no exporté nada. Hay que calibrarla.',
          { ...labelReport(buttons), step: 'error', cause: err.code },
        );
      }
    } else if (!first) {
      capture.cancel({ late: true });
      throw new Stop('export_nothing', '«Exportar chat» no abrió ningún diálogo ni entregó un archivo.');
    }
    const result = await capture.done;
    if (!result.ok && result.reason === 'quarantine') throw quarantineStop();
    if (!result.ok) {
      throw new Stop(
        'export_capture_failed',
        'No pude capturar el archivo exportado por WhatsApp Web.',
        { reason: result.reason },
      );
    }
    // The order WhatsApp Web shows dates in: the server uses it when the
    // export's dates read both ways (10/9 - 10/10, #117).
    return { ok: true, capture: result, dateOrder: dateOrder() };
  }

  // ── Locating a media bubble ──

  /** Elements of `spec` in `row` that are the row's own, not part of a
   * quoted message (a reply quotes someone else's text and name). */
  function ownParts(spec, row) {
    return D.all(spec, row).filter((el) => !el.closest(S.quotedMessage.css));
  }

  function prePlainOf(row) {
    return ownParts(S.prePlainText, row)[0]?.getAttribute('data-pre-plain-text') ?? null;
  }

  /** The loaded rows with the day each belongs to, read afresh (never
   * cached across scans): day separators and rows in DOM order when the
   * separators sit among the rows, else by layout (M.orderDayEntries);
   * text rows' data-pre-plain-text dates anchor their stretch
   * (M.assignDays). `diag` holds counts only (and label shapes, which keep
   * no letter or digit). */
  function dayLayout() {
    const root = D.scroller();
    const empty = { rows: [], oldest: null, sepDates: [], diag: { rows: 0 } };
    if (!root) return empty;
    const order = dateOrder();
    const now = today();
    const entries = [];
    let unparsed = 0;
    const shapes = new Set();
    const query = `${S.messageRow.css}, ${S.daySeparatorCandidates.css}`;
    for (const el of root.querySelectorAll(query)) {
      if (el.matches(S.messageRow.css)) {
        const pre = prePlainOf(el);
        const plain = pre ? M.parsePrePlain(pre, now, L, order) : null;
        entries.push({ row: true, el, anchor: plain?.date ?? null, ambiguous: Boolean(plain?.ambiguous) });
        continue;
      }
      if (el.childElementCount || el.closest(S.messageRow.css)) continue;
      const date = M.parseDayLabel(el.textContent, now, L, order);
      if (date) {
        entries.push({ sep: true, el, date, ambiguous: M.dateAmbiguous(el.textContent) });
      } else if (M.dateLike(el.textContent)) {
        // Not understood: it still ends the day before it, and the rows
        // after it get no day (M.assignDays) rather than a guessed one.
        entries.push({ sep: true, el, date: null, unparsed: true });
        unparsed++;
        if (shapes.size < 5) shapes.add(M.labelShape(el.textContent));
      }
    }
    const { mode, ordered } = M.orderDayEntries(entries);
    let sorted = ordered;
    if (mode === 'layout') {
      // Only now measure (layout reads are the costly part).
      for (const e of entries) e.top = e.el.getBoundingClientRect().top;
      sorted = M.orderDayEntries(entries).ordered;
    }
    const { days, conflicts } = M.assignDays(sorted);
    const rowEntries = sorted.filter((e) => e.row);
    const rows = rowEntries.map((e, i) => ({ el: e.el, day: days[i] }));
    const known = [...days, ...entries.filter((e) => e.sep).map((e) => e.date)].filter(Boolean);
    const sepDates = [...new Set(entries.filter((e) => e.sep && e.date).map((e) => e.date))];
    return {
      rows,
      oldest: known.length ? known.reduce((a, b) => (a < b ? a : b)) : null,
      sepDates,
      diag: {
        day_mode: mode,
        seps: sepDates.length,
        seps_unparsed: unparsed,
        sep_shapes: [...shapes],
        rows: rows.length,
        rows_null_day: days.filter((d) => !d).length,
        anchor_conflicts: conflicts,
      },
    };
  }

  // Ways to ask for older messages, in order (M.pageBack). `top` is the
  // recon's `scrollTop = 0`; it sometimes loads nothing (seen live, e.g.
  // when the list already sits at the top and no scroll event fires), so
  // `first_row` scrolls the oldest row into view after a small nudge down,
  // and `wheel` sends an upward wheel event to the scroller.
  const PAGING_STEPS = ['top', 'first_row', 'wheel'];
  let pagingFallbacks = 0; // per locate: pages that needed a fallback

  async function pagingStep(name, root, firstRow) {
    if (name === 'top') {
      root.scrollTop = 0;
      root.dispatchEvent(new Event('scroll'));
    } else if (name === 'first_row') {
      // A real movement (down, then back up in a later frame) so the
      // browser fires its own scroll events.
      root.scrollTop = Math.min(400, root.scrollHeight);
      await D.sleep(150);
      if (firstRow?.isConnected) firstRow.scrollIntoView({ block: 'start' });
      root.scrollTop = 0;
    } else if (name === 'wheel') {
      root.dispatchEvent(
        new WheelEvent('wheel', { deltaY: -1200, deltaMode: 0, bubbles: true, cancelable: true }),
      );
      root.scrollBy?.(0, -1200);
    }
  }

  /** Page back until the target day is fully loaded (recon §5). Returns
   * {loaded, reason ('max_pages' / 'no_growth' when not), oldest}: the
   * oldest day seen loaded (a separator or a text row's own date). */
  async function loadDay(date, maxPages) {
    let oldest = null;
    for (let page = 0; page <= maxPages; page++) {
      oldest = dayLayout().oldest;
      if (oldest && oldest < date) return { loaded: true, oldest };
      const root = D.scroller();
      const before = D.rows();
      const firstKey = before[0] ? D.rowKey(before[0]) : null;
      const grown = () => {
        const now = D.rows();
        return now.length > before.length || (now[0] && D.rowKey(now[0]) !== firstKey);
      };
      const step = await M.pageBack(
        PAGING_STEPS,
        (name) => pagingStep(name, root, before[0]),
        (name) => D.until(grown, { timeout: name === 'top' ? 6000 : 4000, every: 300 }),
      );
      if (step && step !== 'top') pagingFallbacks++;
      if (!step) {
        // No growth: the beginning of the chat (or a slow load).
        const loaded = Boolean(oldest && oldest <= date);
        return loaded ? { loaded, oldest } : { loaded, reason: 'no_growth', oldest };
      }
    }
    return { loaded: false, reason: 'max_pages', oldest: dayLayout().oldest };
  }

  function kindOf(row) {
    if (D.one(S.voiceNote, row) || D.one(S.audioFile, row)) return 'audio';
    if (D.one(S.documentThumb, row)) return 'document';
    if (D.one(S.videoThumb, row)) return 'video';
    if (D.one(S.imageThumb, row)) return 'image';
    return null;
  }

  function captionOf(row) {
    const texts = D.all(S.selectableText, row).filter((el) => !el.closest(S.quotedMessage.css));
    return texts.length ? texts[0].innerText ?? texts[0].textContent : '';
  }

  /** What a hydrated row shows (no day: the day comes from each scan's
   * layout, never from the cache). */
  function readRow(row, key) {
    const pre = prePlainOf(row);
    const plain = pre ? M.parsePrePlain(pre, today(), L, dateOrder()) : null;
    // The pre-plain sender first; an author label inside a quoted message
    // names the quoted person, never this bubble's sender.
    const author = M.rowAuthor(
      plain?.sender,
      ownParts(S.author, row).map((el) => el.textContent),
    );
    const meta = D.all(S.msgMeta, row).pop();
    const kind = kindOf(row);
    const title = D.one(S.documentTitle, row)?.getAttribute('title');
    return {
      key,
      minutes: M.metaMinutes(meta?.textContent ?? '', plain?.minutes ?? null),
      kind,
      caption: kind ? captionOf(row) : '',
      filename: title ? M.filenameFromTitle(title) : null,
      author,
      runStart: Boolean(D.one(S.runStart, row)),
      outgoing: Boolean(D.one(S.outgoingTail, row)),
    };
  }

  /** Read every message of `date` (chat order), hydrating as needed.
   * Returns {bubbles, others, diag}: `others` are rows read in this run
   * that the current layout puts on another day (for explainMiss). */
  async function scanDay(date) {
    // Hydrate the day's rows first: a text row's pre-plain date can then
    // anchor its stretch in the second layout.
    for (const { el } of dayLayout().rows.filter((r) => r.day === date)) {
      if (!D.hydrated(el)) {
        el.scrollIntoView({ block: 'start' });
        await D.until(() => D.hydrated(el), { timeout: 4000 });
      }
    }
    const layout = dayLayout();
    const infos = [];
    for (const { el, day } of layout.rows) {
      if (day !== date) continue;
      const key = D.rowKey(el);
      if (!key) continue;
      let info = cache.get(key);
      if (!info) {
        if (!D.hydrated(el) && !(await D.hydrate(el))) {
          infos.push({ key, date, unreadable: true });
          continue;
        }
        info = readRow(el, key);
        cache.set(key, info);
      }
      infos.push({ ...info, date });
    }
    const senders = M.propagateSenders(infos);
    const bubbles = infos.map((info, i) => ({ ...info, sender: senders[i] }));
    const others = [];
    for (const { el, day } of layout.rows) {
      if (!day || day === date) continue;
      const info = cache.get(D.rowKey(el));
      if (info) others.push({ ...info, date: day, sender: info.author });
    }
    const diag = {
      ...layout.diag,
      sep_for_day: layout.sepDates.includes(date) ? 1 : 0,
      rows_on_day: infos.length,
    };
    return { bubbles, others, diag };
  }

  async function locate({ group, item, maxPages = 40 }) {
    await ensureChat(group);
    const at = M.itemLocal(item.sent_at);
    if (!at) return { ok: true, skip: 'bad_item' };
    pagingFallbacks = 0;
    const loaded = await loadDay(at.date, maxPages);
    // How many pages needed a paging fallback (a calibration count).
    const paging = { paging_fallbacks: pagingFallbacks };
    if (!loaded.loaded) {
      return {
        ok: true,
        skip: 'not_loaded',
        not_loaded: loaded.reason,
        // Age in days of the oldest day loaded (null: no day was read at all).
        oldest_age: loaded.oldest ? M.daysBetween(loaded.oldest, today()) : null,
        ...paging,
      };
    }
    const { bubbles, others, diag } = await scanDay(at.date);
    const pick = M.chooseBubble(item, bubbles);
    if (pick.skip === 'not_found') {
      // Counts only: the stage that lost the item and near misses.
      return { ok: true, skip: 'not_found', miss: M.explainMiss(item, bubbles, others), diag, ...paging };
    }
    if (pick.skip) return { ok: true, skip: pick.skip, diag, ...paging };
    return { ok: true, key: pick.candidate.key, kind: pick.candidate.kind, ...paging };
  }

  // ── Downloading one bubble ──

  async function download({ group, key }) {
    await ensureChat(group);
    const row = D.rows().find((r) => D.rowKey(r) === key);
    if (!row) return { ok: true, skip: 'row_gone', failure: 'download_row_gone' };
    if (!(await D.hydrate(row))) return { ok: true, skip: 'unreadable' };
    row.scrollIntoView({ block: 'center' });
    await D.sleep(300);
    const bubble = D.one(S.bubble, row) ?? row;
    // A menu or dialog left open (an earlier bubble's context menu, the
    // user's) would offer its own «Descargar»: close it first, or stop.
    if (D.dismissOverlay()) {
      if (!(await D.until(() => !D.overlayOpen(), { timeout: 2000 }))) {
        throw new Stop(
          'stale_menu',
          'Hay un menú o diálogo abierto en WhatsApp Web que no se cierra; ciérralo y vuelve a sincronizar.',
          visibleMenuLabels(),
        );
      }
      await ensureChat(group); // Escape must not have closed the chat
    }
    // Checked before arming: a capture armed for a gone bubble would be
    // left pending.
    if (!bubble.isConnected) return { ok: true, skip: 'row_gone', failure: 'download_row_gone' };
    const capture = D.armCapture(120_000);
    await armed(capture);
    if (!bubble.isConnected) {
      capture.cancel(); // nothing was clicked
      return { ok: true, skip: 'row_gone', failure: 'download_row_gone' };
    }
    D.contextMenu(bubble);
    const items = await D.until(
      () => {
        const found = menuItems(L.download);
        return found.length ? found : null;
      },
      { timeout: MENU_WAIT },
    );
    // Failures of this one bubble come back as a skip (the worker books the
    // message and decides whether to go on); only chat-level problems stop.
    if (!items || items.length !== 1) {
      const report = visibleMenuLabels();
      capture.cancel();
      D.dismissOverlay();
      // Nothing was clicked, so no save can arrive later.
      return {
        ok: true,
        skip: 'download_failed',
        failure: 'download_item_missing',
        detail: { ...report, found: items ? items.length : 0 },
      };
    }
    D.pointerClick(items[0]);
    const result = await capture.done;
    // After the click the menu is normally gone: Escape only if something
    // is still open (with nothing open, Escape closes the chat).
    if (!result.ok && result.reason === 'quarantine') {
      D.dismissOverlay();
      throw quarantineStop();
    }
    if (!result.ok) {
      D.dismissOverlay();
      // `timeout`: the click happened but no save was captured; the page
      // may still save later. `remote_url` / `blob_unreadable`: the save
      // was intercepted under this capture's id, nothing more can come.
      return {
        ok: true,
        skip: 'download_failed',
        failure: result.reason === 'timeout' ? 'download_capture_timeout' : 'download_capture_failed',
        detail: { reason: result.reason },
      };
    }
    return { ok: true, capture: result };
  }

  // ── Router ──

  async function handle(message) {
    switch (message?.type) {
      case 'n16:ping':
        return { ok: true, ready: Boolean(D.one(S.appRoot)) };
      case 'n16:selectChat':
        return selectChat(message);
      case 'n16:beginScan':
        cache = new Map();
        dateOrderSetting = message.dateOrder || 'auto';
        D.abandonCapture();
        D.dropCapture();
        return { ok: true };
      case 'n16:exportChat':
        return exportChat(message);
      case 'n16:locate':
        return locate(message);
      case 'n16:download':
        return download(message);
      case 'n16:readCapture': {
        const chunk = await D.readCapture(message.id, message.offset, message.length);
        return chunk ? { ok: true, ...chunk } : { ok: false, code: 'capture_gone', message: 'El archivo capturado ya no está.' };
      }
      case 'n16:dropCapture':
        D.dropCapture(message.id);
        return { ok: true };
      default:
        return { ok: false, code: 'unknown_command', message: 'Orden desconocida.' };
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!String(message?.type ?? '').startsWith('n16:')) return false;
    handle(message)
      .then(sendResponse)
      .catch((err) => {
        if (err instanceof Stop) {
          sendResponse({ ok: false, code: err.code, message: err.message, detail: err.detail });
        } else {
          console.warn('[n16chat] content script error', err?.name);
          sendResponse({
            ok: false,
            code: 'script_error',
            message: 'Error inesperado en la página de WhatsApp Web.',
          });
        }
      });
    return true;
  });
})();
