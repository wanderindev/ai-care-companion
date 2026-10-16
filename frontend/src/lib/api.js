/**
 * Shared API client. The SPA and the API share one origin (/api/v1), so the
 * session cookie travels automatically and there is no CORS.
 *
 * State-changing requests carry X-Requested-With (CSRF guard, docs/DESIGN.md §8).
 */

const API_PREFIX = '/api/v1'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/**
 * @param {string} path - path under /api/v1, e.g. '/health'
 * @param {{method?: string, body?: unknown, headers?: Record<string,string>, signal?: AbortSignal}} [options]
 */
export async function apiFetch(path, { method = 'GET', body, headers = {}, signal } = {}) {
  const upper = method.toUpperCase()
  const init = {
    method: upper,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...headers },
    signal,
  }

  if (!SAFE_METHODS.has(upper)) {
    init.headers['X-Requested-With'] = 'fetch'
  }

  if (body instanceof FormData) {
    init.body = body
  } else if (body instanceof Blob) {
    // Raw bytes (e.g. one chunk of a WhatsApp export upload).
    init.headers['Content-Type'] = 'application/octet-stream'
    init.body = body
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  const response = await fetch(`${API_PREFIX}${path}`, init)
  const isJson = response.headers.get('content-type')?.includes('application/json')
  // FastAPI labels a 204 as JSON with an empty body, so read the text first:
  // response.json() would throw and turn a successful delete into an error.
  const text = isJson ? await response.text() : ''
  const data = text ? JSON.parse(text) : null

  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : null
    throw new ApiError(response.status, detail || `Error ${response.status}`, data)
  }
  return data
}

export const api = {
  get: (path, options) => apiFetch(path, { ...options, method: 'GET' }),
  post: (path, body, options) => apiFetch(path, { ...options, method: 'POST', body }),
  patch: (path, body, options) => apiFetch(path, { ...options, method: 'PATCH', body }),
  put: (path, body, options) => apiFetch(path, { ...options, method: 'PUT', body }),
  delete: (path, options) => apiFetch(path, { ...options, method: 'DELETE' }),
}
