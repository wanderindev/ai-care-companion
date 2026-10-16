// n16chat sync API client (docs/extension-api.md). Runs in the service
// worker: host_permissions cover app.example.com, the key goes in the
// Authorization header only, no cookies are sent. Never logs the key,
// response bodies or anything about messages.

export class ApiError extends Error {
  constructor(status, detail, code = null) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
    // Stable reason from the X-Error-Code header (e.g. "run_closed").
    this.code = code;
  }
}

/** The key may only travel to n16chat itself or a local dev backend. */
export function isAllowedApiBase(base) {
  let url;
  try {
    url = new URL(base);
  } catch {
    return false;
  }
  if (url.username || url.password) return false;
  if (url.protocol === 'https:' && url.hostname === 'app.example.com' && !url.port) return true;
  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
  return local && (url.protocol === 'http:' || url.protocol === 'https:');
}

/** `base` + `path` + query (undefined / null values dropped). */
export function apiUrl(base, path, query = {}) {
  const url = new URL(String(base).replace(/\/+$/, '') + path);
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
  }
  return url.toString();
}

/** The app's «Pendientes» page for an API base ("https://x/api/v1" →
 * "https://x/pendientes"). The extension's key cannot read the count
 * (it opens only /imports/whatsapp), so the popup links there. */
export function pendientesUrl(base) {
  try {
    return new URL('/pendientes', new URL(base).origin).toString();
  } catch {
    return null;
  }
}

/** Spanish status for an API failure. */
export function describeApiError(err) {
  if (!(err instanceof ApiError)) return 'No se pudo conectar con n16chat.';
  if (err.status === 401) return 'La clave de la extensión no es válida: genera una nueva en n16chat.';
  if (err.status === 403) return 'La clave pertenece a alguien que ya no es administrador.';
  if (err.detail) return err.detail;
  return `n16chat respondió ${err.status}.`;
}

export function isValidKey(token) {
  return /^n16x_[A-Za-z0-9_-]{32,128}$/.test(String(token ?? ''));
}

export function createApi({ apiBase, token }, fetchImpl = fetch) {
  if (!isAllowedApiBase(apiBase)) throw new ApiError(0, 'La dirección de la API no está permitida.');
  async function request(method, path, { query, json, body } = {}) {
    const headers = { Authorization: `Bearer ${token}` };
    if (method !== 'GET') headers['X-Requested-With'] = 'n16chat-extension';
    let payload = body;
    if (json !== undefined) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(json);
    } else if (body instanceof Blob || body instanceof Uint8Array) {
      headers['Content-Type'] = 'application/octet-stream';
    }
    let response;
    try {
      response = await fetchImpl(apiUrl(apiBase, path, query), {
        method,
        headers,
        body: payload,
        credentials: 'omit',
        cache: 'no-store',
      });
    } catch {
      throw new ApiError(0, null);
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : null;
      throw new ApiError(response.status, detail, response.headers.get('X-Error-Code'));
    }
    return { status: response.status, data };
  }

  const base = '/imports/whatsapp';
  return {
    last: async () => (await request('GET', `${base}/last`)).data,
    /** `dateOrder` ("mdy" / "dmy", as WhatsApp Web shows dates) breaks a
     * day/month tie the export alone cannot decide; anything else is left out. */
    createRun: async (size, dateOrder) =>
      (
        await request('POST', base, {
          json: {
            mode: 'daily',
            filename: 'chat.zip',
            size,
            expects_media: true,
            ...(dateOrder === 'mdy' || dateOrder === 'dmy' ? { date_order: dateOrder } : {}),
          },
        })
      ).data,
    putChunk: (id, index, bytes) => request('PUT', `${base}/${id}/chunks/${index}`, { body: bytes }),
    completeUpload: async (id) => (await request('POST', `${base}/${id}/complete`)).data,
    getImport: async (id) => (await request('GET', `${base}/${id}`)).data,
    mediaMissing: async (query) => (await request('GET', `${base}/media-missing`, { query })).data,
    /** Every media-missing item for `query`, paging on `since` (the list is
     * oldest first); stops at `max` items or when a page adds nothing. */
    allMissing: async (query, max = 3000) => {
      const seen = new Map();
      let since = query.since;
      for (;;) {
        const page = (
          await request('GET', `${base}/media-missing`, {
            query: { ...query, since, limit: 1000 },
          })
        ).data;
        let added = 0;
        for (const item of page.items) {
          if (!seen.has(item.id)) {
            seen.set(item.id, item);
            added++;
          }
        }
        if (!page.has_more || !added || seen.size >= max) {
          return { items: [...seen.values()], truncated: Boolean(page.has_more) };
        }
        since = page.items[page.items.length - 1].sent_at;
      }
    },
    uploadMedia: async (id, messageId, blob, filename, waKey) => {
      const form = new FormData();
      form.append('file', blob, filename || 'archivo');
      if (waKey) form.append('wa_key', waKey);
      return request('POST', `${base}/${id}/messages/${messageId}/media`, { body: form });
    },
    completeMedia: async (id) => (await request('POST', `${base}/${id}/media/complete`)).data,
  };
}
