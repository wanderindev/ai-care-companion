import { useQuery } from '@tanstack/react-query'
import { api } from './api.js'

export const PATIENTS_QUERY_KEY = ['patients']

/** Both patients (GET /patients), ordered Papá, Mamá. Signed-in screens only. */
export function usePatientsQuery() {
  return useQuery({
    queryKey: PATIENTS_QUERY_KEY,
    queryFn: () => api.get('/patients'),
    staleTime: 10 * 60_000,
  })
}

/**
 * One color per patient (by slug), the single source for every place that
 * shows both together (calendar dots, patient chips). Blue / orange stay
 * apart for the common color-vision deficiencies, and color always comes
 * with the patient's name or an aria-label, never alone.
 */
const PATIENT_COLORS = {
  papa: { dot: 'bg-blue-600', chip: 'bg-blue-50 text-blue-800 ring-blue-200' },
  mama: { dot: 'bg-orange-500', chip: 'bg-orange-50 text-orange-900 ring-orange-200' },
}
const FALLBACK_COLOR = { dot: 'bg-slate-500', chip: 'bg-slate-100 text-slate-700 ring-slate-200' }

export function patientColor(slug) {
  return PATIENT_COLORS[slug] ?? FALLBACK_COLOR
}
