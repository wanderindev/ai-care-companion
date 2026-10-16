import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'

/**
 * «Pendientes» (DESIGN §6.7 step 5): suggestions from the WhatsApp analysis
 * or the chat, approved / edited / rejected by the family. Approving writes
 * the record through the normal API rules on the server.
 */

export const SUGGESTIONS_KEY = ['suggestions']
const COUNT_KEY = [...SUGGESTIONS_KEY, 'count']

export const KIND_LABELS = {
  create_entry: 'Nueva entrada de bitácora',
  update_entry: 'Cambio en una entrada',
  create_appointment: 'Nueva cita',
  med_change: 'Cambio de medicamento',
  lab_report: 'Informe de laboratorio',
  doctor: 'Nuevo médico',
}

/** Who proposed it: the WhatsApp analysis, the chat, or the doctor inference (#125). */
export const SOURCE_LABELS = {
  whatsapp: 'WhatsApp',
  chat: 'Chat',
  ai: 'IA',
}

export const MED_ACTION_LABELS = {
  add: 'Agregar',
  stop: 'Suspender',
  change: 'Cambiar',
}

export const STATUS_LABELS = {
  pending: 'Pendiente',
  approved: 'Aprobada',
  edited: 'Aprobada con cambios',
  rejected: 'Rechazada',
}

/** Pending suggestions for the selected patients (new doctors always show). */
export function useSuggestions(patientIds, { status = 'pending' } = {}) {
  return useQuery({
    queryKey: [...SUGGESTIONS_KEY, 'list', status, patientIds],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ status, limit: '100' })
      for (const id of patientIds) params.append('patient_id', String(id))
      return api.get(`/suggestions?${params}`, { signal })
    },
  })
}

/** Header badge: the pending count, refreshed every minute and on focus. */
export function usePendingCount() {
  return useQuery({
    queryKey: COUNT_KEY,
    queryFn: ({ signal }) => api.get('/suggestions/count', { signal }),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    select: (data) => data.pending,
  })
}

/** Dry run of a medication change (optionally an edited `payload`). */
export function previewSuggestion(id, payload) {
  return api.post(`/suggestions/${id}/preview`, payload ? { payload } : {})
}

function useDecision(mutationFn) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: SUGGESTIONS_KEY })
      // An approval may have written any of these.
      for (const key of [['log-entries'], ['appointments'], ['meds'], ['doctors']]) {
        queryClient.invalidateQueries({ queryKey: key })
      }
    },
  })
}

/**
 * POST /suggestions/{id}/approve. `body` (all optional): `payload` (edited
 * proposal), `expected_version` + `change_summary` (medications),
 * `doctor_id` (link to an existing doctor).
 */
export function useApproveSuggestion() {
  return useDecision(({ id, body }) => api.post(`/suggestions/${id}/approve`, body ?? {}))
}

export function useRejectSuggestion() {
  return useDecision(({ id, reason }) =>
    api.post(`/suggestions/${id}/reject`, reason ? { reason } : {}),
  )
}

/** '2026-05-01T15:04:00Z' -> '1 may, 10:04' on the Panamá clock. */
export function formatSentAt(iso) {
  return new Intl.DateTimeFormat('es', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'America/Panama',
  }).format(new Date(iso))
}
