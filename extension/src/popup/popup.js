// Popup: «Sincronizar», the current status (polled from the worker's
// persisted run state) and a link to «Pendientes» (the extension's key
// cannot read the count: it only opens /imports/whatsapp).

import { diagnosticDetail, diagnosticLine, leftoverLine, summaryLine } from '../shared/run-helpers.js';

const PHASES = {
  idle: 'Listo para sincronizar.',
  starting: 'Empezando…',
  opening: 'Abriendo WhatsApp Web y el grupo…',
  exporting: 'Exportando el chat (solo texto)…',
  uploading: 'Subiendo el texto a n16chat…',
  importing: 'n16chat está procesando los mensajes…',
  media: 'Buscando y subiendo archivos…',
  completing: 'Cerrando la sincronización…',
  done: 'Sincronización terminada.',
  error: 'La sincronización se detuvo.',
  interrupted: 'La sincronización se interrumpió.',
};

const $ = (id) => document.getElementById(id);

function send(type, payload = {}) {
  return chrome.runtime.sendMessage({ type, ...payload });
}

function render(status) {
  const { run, active, configured, dryRun } = status;
  $('unconfigured').hidden = configured;
  $('main').hidden = !configured;
  $('mode').hidden = !dryRun;
  $('sync').disabled = active;
  $('sync').textContent = dryRun ? 'Sincronizar (prueba)' : 'Sincronizar';
  $('phase').textContent = PHASES[run.phase] ?? run.phase;
  if (run.phase === 'media' && run.mediaListed != null) {
    const c = run.counts ?? {};
    const handled = (c.uploaded ?? 0) + (c.already ?? 0) + (c.captured ?? 0);
    $('phase').textContent += ` (${run.mediaListed} sin archivo, ${handled} procesados)`;
  }
  const finished = ['done', 'error', 'interrupted'].includes(run.phase);
  $('summary').hidden = !finished || run.phase === 'interrupted';
  $('summary').textContent = finished ? summaryLine(run) : '';
  $('leftover').hidden = !finished || !leftoverLine(run);
  $('leftover').textContent = finished ? leftoverLine(run) : '';
  // «Diagnóstico» (#118): locate misses by stage, ids and numbers only.
  const diagnostic = diagnosticLine(run);
  $('diagnostic').hidden = !diagnostic;
  $('diagnostic-line').textContent = diagnostic;
  $('diagnostic-detail').textContent = diagnostic ? diagnosticDetail(run) : '';
  $('error').hidden = !run.error;
  $('error').textContent = run.error ?? '';
  const detail = run.errorDetail ? JSON.stringify(run.errorDetail) : '';
  $('error-detail').hidden = !detail;
  $('error-detail').textContent = detail;
  if (status.pendientesUrl) $('pendientes').href = status.pendientesUrl;
}

async function refresh() {
  const status = await send('sync:status');
  if (status?.ok) render(status);
}

$('sync').addEventListener('click', async () => {
  $('sync').disabled = true;
  const result = await send('sync:start');
  if (!result?.ok) {
    $('error').hidden = false;
    $('error').textContent = result?.error ?? 'No se pudo empezar.';
  }
  refresh();
});

for (const id of ['open-options', 'options-link']) {
  $(id).addEventListener('click', (event) => {
    event.preventDefault();
    chrome.runtime.openOptionsPage();
  });
}

refresh();
setInterval(refresh, 1000);
