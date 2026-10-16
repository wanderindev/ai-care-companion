/**
 * WhatsApp export imports (admin): chunked upload and the list of past imports.
 *
 * A zip of several GB cannot go in one request (the edge allows 200 MB), so
 * `uploadWhatsappExport` creates the import (POST /imports/whatsapp), sends
 * the file in the chunks the server asks for (PUT …/chunks/{i}, raw bytes,
 * retried on network errors), then completes it (POST …/complete), which
 * enqueues the import job. The server keeps every chunk as it arrives, so a
 * retry only resends the chunk that failed.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from './api.js'

export const IMPORTS_KEY = ['whatsapp-imports']

export const MODE_OPTIONS = [
  {
    value: 'labs_only',
    label: 'Solo laboratorios',
    help: 'Guarda todos los mensajes, pero solo los PDF. Para la carga inicial de resultados.',
  },
  {
    value: 'daily',
    label: 'Diario',
    help: 'Mensajes y todos los archivos de los últimos días.',
  },
  {
    value: 'backfill',
    label: 'Histórico',
    help: 'Mensajes y todos los archivos de un periodo largo (por ejemplo, el chat completo). Después se analiza con «Análisis histórico».',
  },
]

export function modeLabel(mode) {
  return MODE_OPTIONS.find((m) => m.value === mode)?.label ?? mode
}

export const STATUS_LABELS = {
  uploading: 'Subiendo',
  queued: 'En cola',
  processing: 'Procesando',
  done: 'Listo',
  failed: 'Falló',
  cancelled: 'Cancelada',
}

export const KIND_LABELS = {
  pdf: 'PDF',
  image: 'fotos',
  audio: 'audios',
  video: 'videos',
  other: 'otros',
}

export const MEDIA_BUCKET_LABELS = {
  uploaded: 'Archivos guardados',
  added_to_existing: 'Archivos añadidos a mensajes ya guardados',
  already_stored: 'Archivos que ya estaban guardados',
  skipped_mode: 'Archivos omitidos por el modo',
  omitted: 'Exportados sin archivo',
  missing: 'No incluidos en el zip',
  too_large: 'Demasiado grandes',
  unsupported: 'Tipo no admitido',
}

export function sumCounts(counts) {
  return Object.values(counts ?? {}).reduce((a, b) => a + b, 0)
}

/** '{pdf: 2, image: 1}' -> '2 PDF, 1 fotos'. */
export function describeCounts(counts) {
  return Object.entries(counts ?? {})
    .map(([kind, n]) => `${n} ${KIND_LABELS[kind] ?? kind}`)
    .join(', ')
}

/** Past imports, latest first (GET /imports/whatsapp). Polls while one is running. */
export function useWhatsappImports() {
  return useQuery({
    queryKey: IMPORTS_KEY,
    queryFn: ({ signal }) => api.get('/imports/whatsapp', { signal }),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((i) => i.status === 'queued' || i.status === 'processing')
        ? 5000
        : false,
  })
}

export function useInvalidateImports() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: IMPORTS_KEY })
}

const CHUNK_RETRIES = 4

function wait(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms)
    signal?.addEventListener('abort', () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    })
  })
}

async function putChunk(id, index, blob, signal) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await api.put(`/imports/whatsapp/${id}/chunks/${index}`, blob, {
        signal,
      })
    } catch (error) {
      // Retry network errors and server hiccups; a 4xx will not get better.
      const retryable = !(error instanceof ApiError) || error.status >= 500
      if (signal?.aborted || !retryable || attempt >= CHUNK_RETRIES) throw error
      await wait(1000 * 2 ** attempt, signal)
    }
  }
}

/**
 * Upload `file` and enqueue its import. `onCreated(import)` fires once the
 * import exists (so the caller can cancel it), `onProgress(fraction)` after
 * each chunk. Resolves to the completed import (`{ id, job_id, … }`).
 */
export async function uploadWhatsappExport(
  file,
  { mode, patientFocusId = null, signal, onCreated, onProgress } = {},
) {
  const created = await api.post(
    '/imports/whatsapp',
    {
      mode,
      patient_focus_id: patientFocusId,
      filename: file.name || 'chat.zip',
      size: file.size,
    },
    { signal },
  )
  onCreated?.(created)
  const { id, chunk_size: chunkSize, chunk_count: chunkCount } = created
  for (let index = 0; index < chunkCount; index += 1) {
    const start = index * chunkSize
    await putChunk(id, index, file.slice(start, Math.min(start + chunkSize, file.size)), signal)
    onProgress?.((index + 1) / chunkCount)
  }
  return api.post(`/imports/whatsapp/${id}/complete`, undefined, { signal })
}

/** DELETE /imports/whatsapp/{id}: abort an unfinished upload. */
export function cancelWhatsappImport(id) {
  return api.delete(`/imports/whatsapp/${id}`)
}

/** ISO timestamp -> '4 mar 2026' in the device's timezone. */
export function formatDay(iso) {
  if (!iso) return ''
  return new Intl.DateTimeFormat('es', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  }).format(new Date(iso))
}

export function formatDateTime(iso) {
  if (!iso) return ''
  return new Intl.DateTimeFormat('es', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso))
}
