import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'

export const EXTENSION_TOKEN_KEY = ['extension-token']

/** Whether the signed-in admin has an active sync-extension token (GET /extension/token). */
export function useExtensionToken() {
  return useQuery({
    queryKey: EXTENSION_TOKEN_KEY,
    queryFn: ({ signal }) => api.get('/extension/token', { signal }),
  })
}

/**
 * POST /extension/token: revokes the old token and returns the new one
 * (`token`, `api_base`). It is the only time the token is shown: only its
 * hash is stored, so keep it in component state, never in the query cache.
 */
export function useGenerateExtensionToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post('/extension/token'),
    onSuccess: (data) =>
      queryClient.setQueryData(EXTENSION_TOKEN_KEY, {
        active: true,
        created_at: data.created_at,
        last_used_at: null,
      }),
  })
}

/** DELETE /extension/token: the extension stops working until a new token is pasted. */
export function useRevokeExtensionToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.delete('/extension/token'),
    onSuccess: () =>
      queryClient.setQueryData(EXTENSION_TOKEN_KEY, {
        active: false,
        created_at: null,
        last_used_at: null,
      }),
  })
}
