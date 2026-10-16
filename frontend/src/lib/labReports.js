/**
 * Labs data hooks over /lab-reports and /analytes (TanStack Query). An edit
 * invalidates the whole `lab-reports` key and every analyte series.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { EMPTY_LAB_FILTERS, toLabApiParams } from './labs.js'

export const LAB_REPORTS_KEY = ['lab-reports']
const SERIES_KEY = ['analyte-series']

const detailKey = (id) => [...LAB_REPORTS_KEY, 'detail', String(id)]
const noRetryOn4xx = (count, error) => !(error?.status >= 400 && error?.status < 500) && count < 1

/** Pages appended with "Cargar más" (phones and desktop alike). */
export function useLabReportsInfinite(filters, patientIds, { enabled = true, pageSize = 30 } = {}) {
  const base = toLabApiParams(filters, patientIds, { limit: pageSize, offset: 0 }).toString()
  return useInfiniteQuery({
    queryKey: [...LAB_REPORTS_KEY, 'infinite', base],
    queryFn: ({ pageParam, signal }) =>
      api.get(
        `/lab-reports?${toLabApiParams(filters, patientIds, { limit: pageSize, offset: pageParam })}`,
        { signal },
      ),
    initialPageParam: 0,
    getNextPageParam: (last) => {
      const next = last.offset + last.items.length
      return next < last.total && last.items.length > 0 ? next : undefined
    },
    enabled,
  })
}

/**
 * How many reports are «Por revisar» for the selected patient(s), unassigned
 * ones included (one-row page: only `total` is used).
 */
export function usePendingLabCount(patientIds, { enabled = true } = {}) {
  const params = toLabApiParams({ ...EMPTY_LAB_FILTERS, pendingOnly: true }, patientIds, {
    limit: 1,
    offset: 0,
  }).toString()
  return useQuery({
    queryKey: [...LAB_REPORTS_KEY, 'pending-count', params],
    queryFn: ({ signal }) => api.get(`/lab-reports?${params}`, { signal }),
    enabled,
  })
}

export function useLabReport(id) {
  return useQuery({
    queryKey: detailKey(id),
    queryFn: ({ signal }) => api.get(`/lab-reports/${id}`, { signal }),
    retry: noRetryOn4xx,
  })
}

/** PATCH a report: `{ id, data }`. Resolves to the saved report. */
export function useUpdateLabReport() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, data }) => api.patch(`/lab-reports/${id}`, data),
    onSuccess: (report) => {
      queryClient.setQueryData(detailKey(report.id), report)
      queryClient.invalidateQueries({ queryKey: LAB_REPORTS_KEY })
      // A patient or date change moves points between series.
      queryClient.invalidateQueries({ queryKey: SERIES_KEY })
    },
  })
}

/**
 * DELETE a report (admins): `id`. Results go with it; the file stays. Drops
 * the detail from the cache and refreshes every list and analyte series.
 */
export function useDeleteLabReport() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/lab-reports/${id}`),
    onSuccess: (_data, id) => {
      queryClient.removeQueries({ queryKey: detailKey(id) })
      queryClient.invalidateQueries({ queryKey: LAB_REPORTS_KEY })
      queryClient.invalidateQueries({ queryKey: SERIES_KEY })
      // Analytes left without results may be gone from «Revisar exámenes».
      queryClient.invalidateQueries({ queryKey: ['analytes'] })
    },
  })
}

export function useAnalyteSeries(analyteId, patientId) {
  return useQuery({
    queryKey: [...SERIES_KEY, String(analyteId), String(patientId)],
    queryFn: ({ signal }) =>
      api.get(`/analytes/${analyteId}/series?patient=${patientId}`, { signal }),
    enabled: Boolean(patientId),
    retry: noRetryOn4xx,
  })
}
