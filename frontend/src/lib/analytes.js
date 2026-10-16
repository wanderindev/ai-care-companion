/**
 * Analyte catalog and its review (admins): GET /analytes, PATCH /analytes/{id}
 * (confirm, rename), POST /analytes/{id}/merge, and the AI triage (#100):
 * GET /analytes/triage/estimate, POST /analytes/triage (job),
 * POST /analytes/{id}/triage/accept|reject, POST /analytes/triage/accept-all.
 * A change invalidates the catalog, every lab report and every analyte series
 * (names shown there).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { LAB_REPORTS_KEY } from './labReports.js'

const ANALYTES_KEY = ['analytes']

export const SPECIMEN_LABELS = { sangre: 'Sangre', orina: 'Orina', heces: 'Heces', otro: 'Otra' }

export function specimenLabel(specimen) {
  return SPECIMEN_LABELS[specimen] ?? specimen
}

export function useAnalytes({ needsReview } = {}) {
  const query = needsReview === undefined ? '' : `?needs_review=${needsReview}`
  return useQuery({
    queryKey: [...ANALYTES_KEY, query],
    queryFn: ({ signal }) => api.get(`/analytes${query}`, { signal }),
  })
}

function useInvalidate() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: ANALYTES_KEY })
    queryClient.invalidateQueries({ queryKey: LAB_REPORTS_KEY })
    queryClient.invalidateQueries({ queryKey: ['analyte-series'] })
  }
}

/** `{ id, data }` → the saved analyte. */
export function useUpdateAnalyte() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({ id, data }) => api.patch(`/analytes/${id}`, data),
    onSuccess: invalidate,
  })
}

/** `{ targetId, sourceId, allowSpecimenMismatch }` → the merged analyte. */
export function useMergeAnalyte() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({ targetId, sourceId, allowSpecimenMismatch = false }) =>
      api.post(`/analytes/${targetId}/merge`, {
        source_id: sourceId,
        allow_specimen_mismatch: allowSpecimenMismatch,
      }),
    onSuccess: invalidate,
  })
}

const TRIAGE_ESTIMATE_KEY = [...ANALYTES_KEY, 'triage-estimate']

export function useTriageEstimate({ enabled = true } = {}) {
  return useQuery({
    queryKey: TRIAGE_ESTIMATE_KEY,
    queryFn: ({ signal }) => api.get('/analytes/triage/estimate', { signal }),
    enabled,
  })
}

/** Queues the triage job → `{ job_id }`. */
export function useStartTriage() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post('/analytes/triage', {}),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: TRIAGE_ESTIMATE_KEY }),
  })
}

/** `id` → the merge target or the confirmed analyte. */
export function useAcceptTriage() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (id) => api.post(`/analytes/${id}/triage/accept`, {}),
    onSuccess: invalidate,
  })
}

export function useRejectTriage() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (id) => api.post(`/analytes/${id}/triage/reject`, {}),
    onSuccess: invalidate,
  })
}

/** → `{ accepted, failed }`. */
export function useAcceptAllTriage() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: () => api.post('/analytes/triage/accept-all', {}),
    onSuccess: invalidate,
  })
}

export const TRIAGE_CONFIDENCE_LABELS = { high: 'alta', medium: 'media', low: 'baja' }

export const TRIAGE_REASON_LABELS = {
  same_test_spelling: 'Mismo examen escrito de otra forma',
  same_test_abbreviation: 'Mismo examen, abreviado',
  same_test_translation: 'Mismo examen en otro idioma',
  same_test_synonym: 'Mismo examen con otro nombre',
  new_test: 'Examen que aún no está en la lista',
  different_specimen: 'Otra muestra (sangre, orina…)',
  different_test: 'Parecido, pero es otro examen',
  not_a_test: 'No parece un examen',
  ambiguous: 'Nombre ambiguo',
  insufficient_data: 'Faltan datos para decidir',
  invalid_target: 'La propuesta no era válida',
}

/** Accent/case-insensitive match on the name or an alias. */
export function analyteMatches(analyte, query) {
  const fold = (s) =>
    (s ?? '')
      .normalize('NFD')
      .replace(/[̀-ͯ]/g, '')
      .toLowerCase()
  const needle = fold(query.trim())
  if (!needle) return true
  return [analyte.canonical_name, ...analyte.aliases].some((n) => fold(n).includes(needle))
}
