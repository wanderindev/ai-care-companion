import { useQuery } from '@tanstack/react-query'
import { api } from './api.js'

/** Spanish labels for a background job's status (GET /jobs/{id}). */
export const JOB_STATUS_LABELS = {
  queued: 'En cola',
  running: 'Procesando…',
  done: 'Listo',
  failed: 'No se pudo completar',
}

export const JOB_POLL_MS = 1500

export function isJobFinished(job) {
  return job?.status === 'done' || job?.status === 'failed'
}

/**
 * Poll a background job until it is done or failed (GET /jobs/{id}).
 * Pass `null`/`undefined` to stay idle. `data` is the job:
 * `{ id, kind, status, progress, message, attempts, max_attempts, error }`.
 */
export function useJob(jobId) {
  return useQuery({
    queryKey: ['jobs', jobId],
    queryFn: ({ signal }) => api.get(`/jobs/${jobId}`, { signal }),
    enabled: jobId != null,
    refetchInterval: (query) => (isJobFinished(query.state.data) ? false : JOB_POLL_MS),
    refetchIntervalInBackground: false,
    staleTime: 0,
  })
}
