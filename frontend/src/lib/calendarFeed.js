import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'

export const CALENDAR_FEED_KEY = ['calendar-feed']

/** Whether the signed-in user has an active ICS feed (GET /calendar/feed). */
export function useCalendarFeed() {
  return useQuery({
    queryKey: CALENDAR_FEED_KEY,
    queryFn: ({ signal }) => api.get('/calendar/feed', { signal }),
  })
}

/**
 * POST /calendar/feed: revokes the old link and returns the new one
 * (`url`, `webcal_url`). It is the only time the link is shown: only its
 * hash is stored, so keep it in component state, never in the query cache.
 */
export function useGenerateCalendarFeed() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post('/calendar/feed'),
    onSuccess: (data) =>
      queryClient.setQueryData(CALENDAR_FEED_KEY, {
        active: true,
        created_at: data.created_at,
      }),
  })
}

/** DELETE /calendar/feed: turns the link off. */
export function useRevokeCalendarFeed() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.delete('/calendar/feed'),
    onSuccess: () =>
      queryClient.setQueryData(CALENDAR_FEED_KEY, { active: false, created_at: null }),
  })
}
