// Options page: API base, the n16x_ key, the group name and run limits,
// stored in chrome.storage.local through the worker. The saved key is
// never shown again (the field stays empty: leave it empty to keep it).

const $ = (id) => document.getElementById(id);
const FIELDS = ['apiBase', 'groupName', 'maxFiles', 'retryDays', 'maxPages', 'dateOrder'];

function send(type, payload = {}) {
  return chrome.runtime.sendMessage({ type, ...payload });
}

async function load() {
  const { settings } = await send('settings:get');
  for (const field of FIELDS) $(field).value = settings[field];
  $('dryRun').checked = Boolean(settings.dryRun);
  $('token').placeholder = settings.hasToken ? 'Guardada (escribe otra para cambiarla)' : 'n16x_…';
  $('origin').textContent = `chrome-extension://${chrome.runtime.id}`;
  $('skip-count').textContent = `Archivos omitidos en espera: ${settings.skipCount ?? 0}.`;
  $('clear-skips').disabled = !settings.skipCount;
}

$('clear-skips').addEventListener('click', async () => {
  const result = await send('skips:clear');
  $('skip-status').textContent = result?.ok
    ? 'Listo: la próxima sincronización los vuelve a intentar.'
    : result?.error ?? 'No se pudo vaciar la lista.';
  load();
});

$('form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const token = $('token').value.trim();
  if (token && !/^n16x_[A-Za-z0-9_-]{32,128}$/.test(token)) {
    $('status').textContent = 'La clave debe empezar con n16x_ (cópiala completa de n16chat).';
    return;
  }
  const patch = {
    apiBase: $('apiBase').value.trim().replace(/\/+$/, ''),
    groupName: $('groupName').value.trim(),
    dryRun: $('dryRun').checked,
    maxFiles: Math.max(1, Math.min(200, Number($('maxFiles').value) || 25)),
    retryDays: Math.max(1, Math.min(30, Number($('retryDays').value) || 14)),
    maxPages: Math.max(1, Math.min(200, Number($('maxPages').value) || 40)),
    dateOrder: $('dateOrder').value,
    token,
  };
  const saved = await send('settings:set', { patch });
  if (!saved?.ok) {
    $('status').textContent = saved?.error ?? 'No se pudo guardar.';
    return;
  }
  $('token').value = '';
  $('status').textContent = 'Guardado.';
  load();
});

$('test').addEventListener('click', async () => {
  $('status').textContent = 'Probando…';
  const result = await send('settings:test');
  $('status').textContent = result.ok
    ? `Conexión correcta. Último mensaje guardado: ${result.lastMessageAt ?? 'ninguno'}.`
    : result.error;
});

load();
