import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { normalizeText } from './bitacora.js'
import { LOG_ENTRIES_KEY } from './logEntries.js'

export const DOCTORS_QUERY_KEY = ['doctors']

/** Every doctor, by name (GET /doctors). A family has dozens, not thousands. */
export function useDoctors() {
  return useQuery({
    queryKey: DOCTORS_QUERY_KEY,
    queryFn: ({ signal }) => api.get('/doctors', { signal }),
    staleTime: 5 * 60_000,
  })
}

/** POST /doctors. Rejects with ApiError 409 when the name/alias already exists. */
export function useCreateDoctor() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/doctors', data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: DOCTORS_QUERY_KEY }),
  })
}

/** Doctors whose name or alias matches `text` (GET /doctors?match=). */
export function matchDoctors(text) {
  return api.get(`/doctors?match=${encodeURIComponent(text)}`)
}

function useDoctorWrite(mutationFn) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    // Entries embed the doctor's name: refresh them too.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: DOCTORS_QUERY_KEY })
      queryClient.invalidateQueries({ queryKey: LOG_ENTRIES_KEY })
    },
  })
}

/** PATCH /doctors/{id} with the changed fields. A 409 detail names the conflicting doctor. */
export function useUpdateDoctor() {
  return useDoctorWrite(({ id, data }) => api.patch(`/doctors/${id}`, data))
}

/**
 * Admins only: POST /doctors/{targetId}/merge. The duplicate's entries move to
 * the target, its name and aliases become aliases, and the duplicate is deleted.
 */
export function useMergeDoctor() {
  return useDoctorWrite(({ targetId, sourceId }) =>
    api.post(`/doctors/${targetId}/merge`, { source_id: sourceId }),
  )
}

/** `tel:` link for a phone as the family typed it ("6000-1111", "+507 6000 1111"). */
export function telHref(phone) {
  return `tel:${phone.replace(/[^\d+]/g, '')}`
}

/** Client-side search: name or any alias contains the text (accent/case-insensitive). */
export function doctorMatches(doctor, query) {
  const needle = normalizeText(query)
  if (!needle) return true
  return [doctor.name, ...doctor.aliases].some((t) => normalizeText(t).includes(needle))
}
