// WhatsApp Web selector and label registry: the ONLY place selectors and UI
// labels live. Source: docs/whatsapp-web-recon.md (#44, captured on an
// English UI). Every entry marked `verified: false` is a guess that the
// first calibration run (README, "Calibración") must confirm or fix; the
// Spanish labels are all unverified.
//
// Selector specs: { css, text?, description, verified }. `text` is a regex
// tested against the element's normalized textContent / aria-label / title.
// WhatsApp's class names are obfuscated: never use them.
//
// Loaded as a classic content script (globalThis.N16.SELECTORS /
// N16.LABELS) and imported by the node tests; no DOM access here.

(() => {
  const N16 = (globalThis.N16 ??= {});

  N16.SELECTORS = {
    // ── App and chat list (recon §1) ──
    appRoot: {
      css: '[data-testid="wa-web-main-screen"]',
      description: 'WhatsApp Web main screen (absent while the QR / loading screen shows)',
      verified: true,
    },
    chatSearch: {
      css: '#side input[role="textbox"], [data-testid="chat-list-search-container"] input',
      description: 'chat list search input (a plain <input>, never the composer)',
      verified: true,
    },
    chatListRow: {
      css: '[role="grid"] [role="row"]',
      description: 'chat list row (virtualized: only ~18 rendered)',
      verified: true,
    },
    chatRowTitle: {
      css: '[data-testid="cell-frame-title"] [title]',
      description: 'chat row title; its title attribute is the full chat name',
      verified: true,
    },
    chatRowClickable: {
      // The outer gridcell ignores synthetic clicks; the frame container
      // opens the chat (calibrated live 2026-10-09).
      css: '[data-testid="cell-frame-container"]',
      description: 'clickable part of a chat row (falls back to the title)',
      verified: true,
    },
    mainPane: {
      css: '#main',
      description: 'open chat pane',
      verified: true,
    },
    headerTitle: {
      css: '[data-testid="conversation-info-header-chat-title"]',
      description: 'open chat name in the header',
      verified: true,
    },
    composeBox: {
      css: '[data-testid="compose-box"], footer',
      description: 'message composer: the extension never types or clicks inside it',
      verified: true,
    },
    composeInput: {
      css: '[data-testid="conversation-compose-box-input"]',
      description: 'composer input; its aria-label names the open group',
      verified: true,
    },

    // ── Chat menu and export (recon §1: unverified path) ──
    headerMenuButton: {
      css: '[data-testid="conversation-header"] [role="button"][aria-label], [data-testid="conversation-header"] button[aria-label]',
      text: /^(menu|menú|more options|más opciones)$/i,
      description: 'chat header "Menu" button',
      verified: true,
    },
    menuItem: {
      css: '[role="application"] [role="button"], [role="menu"] [role="menuitem"], [role="menu"] [role="button"], li[role="button"], div[role="menuitem"], li[data-animate-dropdown-item] div',
      description: 'an item of an open WhatsApp dropdown / context menu',
      verified: false,
    },
    dialog: {
      css: '[role="dialog"], [data-animate-modal-popup="true"]',
      description: 'modal dialog (export confirmation)',
      verified: false,
    },
    // Export dialog (calibrated live 2026-10-09, English UI): a radio group
    // "All messages" / "Custom date range"; custom shows two native date
    // inputs (From / To, max = today).
    exportRangeAll: {
      css: 'input[type="radio"][name="date-range"][value="all"]',
      description: 'export dialog: "All messages" radio',
      verified: true,
    },
    exportRangeCustom: {
      css: 'input[type="radio"][name="date-range"][value="custom"]',
      description: 'export dialog: "Custom date range" radio',
      verified: true,
    },
    exportDateInput: {
      css: 'input[type="date"]',
      description: 'export dialog: any native date input (exactly two: From / To)',
      verified: true,
    },
    // Taken by aria-label, never by DOM order (#102): a reordered dialog
    // must not swap the range. English labels from the calibration review;
    // the Spanish ones are unverified candidates.
    exportDateFrom: {
      css: 'input[type="date"]',
      text: /^(start date|from|fecha de inicio|fecha inicial|desde)$/i,
      description: 'export dialog: "From" date input (aria-label "Start date")',
      verified: false,
    },
    exportDateTo: {
      css: 'input[type="date"]',
      text: /^(end date|to|fecha de finalización|fecha de finalizacion|fecha final|fecha de fin|hasta)$/i,
      description: 'export dialog: "To" date input (aria-label "End date")',
      verified: false,
    },
    dialogButton: {
      css: 'button, [role="button"]',
      description: 'a button inside the modal dialog',
      verified: false,
    },

    // ── Message list (recon §2, §5) ──
    messagesScroller: {
      css: '[data-testid="conversation-panel-messages"]',
      description: 'the element that scrolls the message list',
      verified: true,
    },
    messageRow: {
      css: '[role="row"]',
      description: 'one message row inside the scroller',
      verified: true,
    },
    messageKey: {
      css: 'div[data-id]',
      description: 'element holding the 32-hex message key (data-id)',
      verified: true,
    },
    virtualizedPlaceholder: {
      css: '[data-virtualized="true"]',
      description: 'dehydrated row content (scroll it into view first)',
      verified: true,
    },
    bubble: {
      css: '[data-testid="msg-container"]',
      description: 'message bubble (target of the context menu)',
      verified: true,
    },
    author: {
      css: '[data-testid="author"]',
      description: 'sender label (first bubble of a run only)',
      verified: true,
    },
    runStart: {
      css: '[data-icon="tail-in"]',
      description: 'bubble tail: first incoming bubble of a sender run',
      verified: true,
    },
    outgoingTail: {
      css: '[data-icon="tail-out"]',
      description: 'bubble tail of an own (outgoing) message',
      verified: false,
    },
    msgMeta: {
      css: '[data-testid="msg-meta"]',
      description: 'bubble time (h:mm, minutes only)',
      verified: true,
    },
    prePlainText: {
      css: '[data-pre-plain-text]',
      description: 'text-message container with "[h:mm AM, M/D/YYYY] name: "',
      verified: true,
    },
    selectableText: {
      css: '[data-testid="selectable-text"]',
      description: 'message / caption text (skip the one inside a quote)',
      verified: true,
    },
    quotedMessage: {
      css: '[data-testid="quoted-message"]',
      description: 'quoted part of a reply',
      verified: true,
    },
    daySeparatorCandidates: {
      css: 'span',
      description: 'leaf spans that may be day separators (Today / a weekday / a date); never inside a row',
      verified: true,
    },

    // ── Media kinds (recon §2 "Message kinds") ──
    voiceNote: {
      css: '[data-icon="ptt-status"], [data-testid="ptt-status"], button[aria-label="Play voice message"], button[aria-label="Reproducir mensaje de voz"]',
      description: 'voice note bubble markers',
      verified: true,
    },
    audioFile: {
      css: '[data-testid="audio-play"], [data-icon="audio-play"]',
      description: 'non-voice audio file bubble',
      verified: false,
    },
    imageThumb: {
      css: '[data-testid="image-thumb"]',
      description: 'image bubble',
      verified: true,
    },
    renderedImage: {
      css: 'img[src^="blob:"]',
      description: 'rendered image blob (fallback only: maybe not the original bytes)',
      verified: true,
    },
    documentThumb: {
      css: '[data-testid="document-thumb"]',
      description: 'document bubble',
      verified: true,
    },
    documentTitle: {
      css: 'div[title^="View \\""], div[title^="Ver \\""], div[title^="Abrir \\""]',
      description: 'document bubble element whose title holds the file name',
      verified: false,
    },
    videoThumb: {
      css: '[data-testid="video-thumb"], [data-testid="media-play"], [data-icon="media-play"]',
      description: 'video / GIF bubble',
      verified: false,
    },
  };

  // UI labels (lower-cased comparison after normalization). English from the
  // recon; Spanish unverified.
  N16.LABELS = {
    exportChat: [/^export chat$/i, /^exportar chat$/i],
    moreSubmenu: [/^more$/i, /^más$/i],
    exportConfirm: [/^export$/i, /^exportar$/i, /^without media$/i, /^sin archivos$/i],
    download: [/^download$/i, /^descargar$/i],
    cancel: [/^cancel$/i, /^cancelar$/i],
    // Every menu item / dialog button / date field label a stop report may
    // carry (#102). A stop records only labels matching one of these (and
    // the count of the others): a menu item such as «Message <name>», a
    // button named after a file or any label never seen before could carry
    // a sender or file name into the run state. Add a label here when a
    // calibration report needs it.
    known: [
      // Chat header menu.
      /^(group info|contact info|info\. del grupo|info\. del contacto|información del grupo|información del contacto)$/i,
      /^(select messages|seleccionar mensajes|mute notifications|silenciar notificaciones)$/i,
      /^(disappearing messages|mensajes temporales|add to favorites|remove from favorites)$/i,
      /^(añadir a favoritos|agregar a favoritos|eliminar de favoritos|add to list|añadir a la lista|agregar a la lista)$/i,
      /^(close chat|cerrar chat|clear chat|vaciar chat|delete chat|eliminar chat)$/i,
      /^(exit group|salir del grupo|report|reportar|denunciar|block|bloquear)$/i,
      /^(export chat|exportar chat|more|más)$/i,
      // Message context menu.
      /^(reply|responder|reply privately|responder en privado|react|reaccionar)$/i,
      /^(download|descargar|forward|reenviar|copy|copiar|pin|fijar|unpin|desfijar|dejar de fijar)$/i,
      /^(star|unstar|destacar|no destacar|delete|eliminar|edit|editar|keep|conservar)$/i,
      /^(message info|info\. del mensaje|información del mensaje|select|seleccionar|save as|guardar como)$/i,
      // Dialog buttons and fields.
      /^(cancel|cancelar|export|exportar|without media|sin archivos|include media|incluir archivos)$/i,
      /^(ok|aceptar|close|cerrar|all messages|todos los mensajes|custom date range|rango de fechas personalizado)$/i,
      /^(start date|end date|from|to|fecha de inicio|fecha inicial|fecha de finalización|fecha final|fecha de fin|desde|hasta)$/i,
    ],
    // Day separators (recon §2): Today / Yesterday / a weekday / a date.
    today: ['today', 'hoy'],
    yesterday: ['yesterday', 'ayer'],
    weekdays: [
      ['sunday', 'domingo'],
      ['monday', 'lunes'],
      ['tuesday', 'martes'],
      ['wednesday', 'miércoles', 'miercoles'],
      ['thursday', 'jueves'],
      ['friday', 'viernes'],
      ['saturday', 'sábado', 'sabado'],
    ],
    months: [
      ['january', 'enero'],
      ['february', 'febrero'],
      ['march', 'marzo'],
      ['april', 'abril'],
      ['may', 'mayo'],
      ['june', 'junio'],
      ['july', 'julio'],
      ['august', 'agosto'],
      ['september', 'septiembre', 'setiembre'],
      ['october', 'octubre'],
      ['november', 'noviembre'],
      ['december', 'diciembre'],
    ],
  };
})();
