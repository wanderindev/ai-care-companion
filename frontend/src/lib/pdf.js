import { ApiError } from './api.js'

function pdfFilename(response, fallback) {
  const match = /filename="([^"]+)"/.exec(response.headers.get('content-disposition') ?? '')
  return match?.[1] ?? fallback
}

/**
 * GET a PDF under /api/v1 as a `File` (PDF bytes + the server's file name).
 * Fetched with the session cookie rather than linked: an iPhone home-screen
 * app opens links in a separate browser view that has no session.
 */
export async function fetchPdf(path, { signal, fallbackName = 'documento.pdf' } = {}) {
  const response = await fetch(`/api/v1${path}`, { credentials: 'same-origin', signal })
  if (!response.ok) {
    const body = response.headers.get('content-type')?.includes('application/json')
      ? await response.json()
      : null
    const detail = typeof body?.detail === 'string' ? body.detail : null
    throw new ApiError(response.status, detail || `Error ${response.status}`, body)
  }
  const blob = await response.blob()
  return new File([blob], pdfFilename(response, fallbackName), { type: 'application/pdf' })
}
