/**
 * Bitácora data hooks over /log-entries (TanStack Query). Every write
 * invalidates the whole `log-entries` key, so lists and details refetch.
 */
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { api } from './api.js'
import { toApiParams } from './bitacora.js'

export const LOG_ENTRIES_KEY = ['log-entries']

const detailKey = (id) => [...LOG_ENTRIES_KEY, 'detail', String(id)]

/** Mobile: pages appended with "Cargar más". */
export function useLogEntriesInfinite(filters, patientIds, { enabled = true, pageSize = 20 } = {}) {
  const base = toApiParams(filters, patientIds, { limit: pageSize, offset: 0 }).toString()
  return useInfiniteQuery({
    queryKey: [...LOG_ENTRIES_KEY, 'infinite', base],
    queryFn: ({ pageParam, signal }) =>
      api.get(
        `/log-entries?${toApiParams(filters, patientIds, { limit: pageSize, offset: pageParam })}`,
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

/** Desktop: one numbered page at a time (the previous page stays while loading). */
export function useLogEntriesPage(filters, patientIds, { enabled = true, pageSize = 25 } = {}) {
  const params = toApiParams(filters, patientIds, {
    limit: pageSize,
    offset: (filters.page - 1) * pageSize,
  }).toString()
  return useQuery({
    queryKey: [...LOG_ENTRIES_KEY, 'page', params],
    queryFn: ({ signal }) => api.get(`/log-entries?${params}`, { signal }),
    placeholderData: keepPreviousData,
    enabled,
  })
}

export function useLogEntry(id) {
  return useQuery({
    queryKey: detailKey(id),
    queryFn: ({ signal }) => api.get(`/log-entries/${id}`, { signal }),
    retry: (count, error) => !(error?.status >= 400 && error?.status < 500) && count < 1,
  })
}

function useEntryWrite(mutationFn) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: (entry) => {
      queryClient.setQueryData(detailKey(entry.id), entry)
      queryClient.invalidateQueries({ queryKey: LOG_ENTRIES_KEY })
      // Saving an entry can create/change/cancel its "Próxima cita".
      queryClient.invalidateQueries({ queryKey: ['appointments'] })
    },
  })
}

/** Create (no `id`) or update (`id`) an entry. Resolves to the saved entry. */
export function useSaveLogEntry() {
  return useEntryWrite(({ id, data }) =>
    id ? api.patch(`/log-entries/${id}`, data) : api.post('/log-entries', data),
  )
}

export function useMarkReviewed() {
  return useEntryWrite((id) => api.patch(`/log-entries/${id}`, { review_status: 'ok' }))
}

/** Admins: mark every «Por revisar» entry matching `filter` (`toReviewFilter`) as reviewed. */
export function useBulkReview() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (filter) => api.post('/log-entries/review', { filter }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: LOG_ENTRIES_KEY }),
  })
}

export function useDeleteLogEntry() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/log-entries/${id}`),
    // The deleted entry's own detail query is left alone: refetching it while
    // its page is still mounted would flash "not found" before navigating away.
    onSuccess: (_data, id) => {
      const gone = detailKey(id).join('/')
      queryClient.invalidateQueries({
        queryKey: LOG_ENTRIES_KEY,
        predicate: (query) => query.queryKey.join('/') !== gone,
      })
    },
  })
}
