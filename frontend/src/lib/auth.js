/**
 * Auth hooks over the magic-link API (docs/DESIGN.md §2). The session lives in
 * an HttpOnly cookie, so the SPA only ever asks the server who is signed in.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from './api.js'

export const ME_QUERY_KEY = ['auth', 'me']

/** Current user, or an ApiError with status 401 when signed out. */
export function useMe() {
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: () => api.get('/auth/me'),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 1,
    staleTime: 5 * 60_000,
  })
}

export function useRequestLink() {
  return useMutation({
    mutationFn: (email) => api.post('/auth/request-link', { email }),
  })
}

export function useVerify() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (token) => api.post('/auth/verify', { token }),
    onSuccess: (user) => queryClient.setQueryData(ME_QUERY_KEY, user),
  })
}

export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post('/auth/logout'),
    // Drop every cached response (medical data) whatever the outcome.
    onSettled: () => queryClient.clear(),
  })
}

/** Reads the magic-link token from the URL fragment (`/acceso#token=…`). */
export function readTokenFromHash(hash = window.location.hash) {
  const params = new URLSearchParams(hash.replace(/^#/, ''))
  return params.get('token')
}
