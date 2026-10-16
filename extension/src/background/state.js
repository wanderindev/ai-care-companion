// Persistent state in chrome.storage.local. The MV3 service worker can be
// killed at any moment, so every run step is written here at once.
// Settings hold the API key (never in code); the run state holds counts,
// ids and Spanish statuses only — never message text, senders or file names.

const SETTINGS_KEY = 'n16Settings';
const RUN_KEY = 'n16Run';
const MEMORY_KEY = 'n16Memory';

export const DEFAULT_SETTINGS = {
  apiBase: 'https://app.example.com/api/v1',
  token: '',
  groupName: '',
  // A fresh install exports and locates but uploads nothing.
  dryRun: true,
  maxFiles: 25,
  retryDays: 14, // earlier runs' missing files are retried while this recent
  maxPages: 40,
  dateOrder: 'auto', // day separators: auto / mdy / dmy
};

export async function getSettings() {
  const stored = await chrome.storage.local.get(SETTINGS_KEY);
  return { ...DEFAULT_SETTINGS, ...(stored[SETTINGS_KEY] ?? {}) };
}

export async function patchSettings(patch) {
  const current = await getSettings();
  await chrome.storage.local.set({ [SETTINGS_KEY]: { ...current, ...patch } });
}

export async function getRun() {
  const stored = await chrome.storage.local.get(RUN_KEY);
  return stored[RUN_KEY] ?? { phase: 'idle' };
}

export async function setRun(run) {
  await chrome.storage.local.set({ [RUN_KEY]: run });
}

export async function patchRun(patch) {
  const run = await getRun();
  const next = { ...run, ...patch, heartbeatAt: Date.now() };
  await setRun(next);
  return next;
}

/** What the extension remembers between runs: when the last real run
 * finished, and the skip book (message ids only, see run-helpers.js). */
export async function getMemory() {
  const stored = await chrome.storage.local.get(MEMORY_KEY);
  return { lastSuccessAt: null, skips: {}, ...(stored[MEMORY_KEY] ?? {}) };
}

export async function patchMemory(patch) {
  const memory = await getMemory();
  await chrome.storage.local.set({ [MEMORY_KEY]: { ...memory, ...patch } });
}
