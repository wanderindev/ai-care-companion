// Service worker: message router for the popup and the options page.
// The sync run itself lives in run.js; state persists in state.js.

import { getMemory, getRun, getSettings, patchMemory, patchRun, patchSettings } from './state.js';
import { isActive, startSync } from './run.js';
import { isOwnPage, isStale, pruneSkips } from '../shared/run-helpers.js';
import { createApi, describeApiError, isAllowedApiBase, isValidKey, pendientesUrl } from './api.js';

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Only the extension's own pages (popup, options) talk to the worker.
  const origin = new URL(chrome.runtime.getURL('')).origin;
  if (!isOwnPage(sender, chrome.runtime.id, origin)) return false;
  dispatch(message)
    .then(sendResponse)
    .catch(() => sendResponse({ ok: false, error: 'Error interno de la extensión.' }));
  return true;
});

async function dispatch(message) {
  switch (message?.type) {
    case 'sync:start': {
      if (isActive()) return { ok: false, error: 'Ya hay una sincronización en curso.' };
      startSync(); // runs on; the popup follows the persisted state
      return { ok: true };
    }
    case 'sync:status': {
      let run = await getRun();
      if (!isActive() && isStale(run)) {
        run = await patchRun({
          phase: 'interrupted',
          error: 'La sincronización se interrumpió (se cerró el navegador o la extensión). Puedes repetirla: no duplica nada.',
        });
      }
      const settings = await getSettings();
      return {
        ok: true,
        run,
        active: isActive(),
        dryRun: settings.dryRun,
        configured: Boolean(settings.token && settings.groupName),
        pendientesUrl: pendientesUrl(settings.apiBase),
      };
    }
    case 'settings:get': {
      const settings = await getSettings();
      const { skips } = await getMemory();
      // The key never goes back to a page in full.
      return {
        ok: true,
        settings: {
          ...settings,
          token: '',
          hasToken: Boolean(settings.token),
          skipCount: Object.keys(pruneSkips(skips, Date.now())).length,
        },
      };
    }
    case 'skips:clear': {
      // «Reintentar archivos omitidos»: a fixed version retries everything.
      if (isActive()) return { ok: false, error: 'Espera a que termine la sincronización en curso.' };
      await patchMemory({ skips: {} });
      return { ok: true };
    }
    case 'settings:set': {
      const patch = { ...message.patch };
      if (!patch.token) delete patch.token;
      if (patch.token && !isValidKey(patch.token)) return { ok: false, error: 'Clave no válida.' };
      if ('apiBase' in patch && !isAllowedApiBase(patch.apiBase)) {
        return {
          ok: false,
          error: 'Dirección no permitida: usa https://app.example.com/api/v1 o un servidor local.',
        };
      }
      await patchSettings(patch);
      return { ok: true };
    }
    case 'settings:test': {
      const settings = await getSettings();
      if (!isValidKey(settings.token)) return { ok: false, error: 'Falta una clave n16x_… válida.' };
      try {
        const last = await createApi(settings).last();
        return { ok: true, lastMessageAt: last.last_message_at };
      } catch (err) {
        return { ok: false, error: describeApiError(err) };
      }
    }
    default:
      return { ok: false, error: 'Mensaje desconocido.' };
  }
}
