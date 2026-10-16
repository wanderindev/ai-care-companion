/**
 * Lab portal links shared in the chat (docs/DESIGN.md §6.5, #16): the
 * «Enlaces pendientes» list on /labs. The URL is shown as text and opened in
 * a new tab only; it may carry a patient token, so it is never logged.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'

export const LAB_LINKS_KEY = ['lab-links']

const REASONS = {
  not_allowed: 'Hay que abrirlo a mano.',
  blocked: 'No se descargó por seguridad; ábrelo a mano.',
  not_pdf: 'El enlace no lleva directo al PDF (quizá pide iniciar sesión).',
  http_error: 'La página del laboratorio respondió con un error.',
  too_large: 'El PDF es demasiado grande para descargarlo solo.',
  too_many_redirects: 'El enlace redirige demasiadas veces.',
  unreachable: 'El laboratorio no respondió.',
}

/** Why a pending link was not downloaded, in Spanish. */
export function linkReasonLabel(reason) {
  if (!reason) return 'Intentando descargar el PDF…'
  return REASONS[reason] ?? 'No se pudo descargar el PDF.'
}

/** Only http(s) URLs become links; anything else is never put in an href. */
export function safeHref(url) {
  return /^https?:\/\//i.test(url ?? '') ? url : null
}

/** The message's date and time, e.g. '4 mar 2026, 14:30'. */
export function formatSentAt(iso) {
  if (!iso) return ''
  return new Intl.DateTimeFormat('es', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso))
}

export function usePendingLabLinks({ enabled = true } = {}) {
  return useQuery({
    queryKey: [...LAB_LINKS_KEY, 'pending'],
    queryFn: ({ signal }) => api.get('/lab-links?status=pending', { signal }),
    enabled,
  })
}

export function useIgnoreLabLink() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.post(`/lab-links/${id}/ignore`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: LAB_LINKS_KEY }),
  })
}
