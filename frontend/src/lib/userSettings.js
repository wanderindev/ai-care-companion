import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'

export const USER_SETTINGS_KEY = ['me-settings']

/** The signed-in user's own settings (GET /me/settings): `email`, `email_invites`. */
export function useUserSettings() {
  return useQuery({
    queryKey: USER_SETTINGS_KEY,
    queryFn: ({ signal }) => api.get('/me/settings', { signal }),
  })
}

/** PATCH /me/settings with the fields to change (e.g. `{ email_invites: true }`). */
export function useUpdateUserSettings() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (changes) => api.patch('/me/settings', changes),
    onSuccess: (data) => queryClient.setQueryData(USER_SETTINGS_KEY, data),
  })
}
