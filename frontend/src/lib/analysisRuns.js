/**
 * Backfill analysis runs (admin, «Análisis histórico» on Importar WhatsApp):
 * the stored messages of a date range → suggestions for one patient.
 *
 * POST /analysis-runs/estimate (no cost), POST /analysis-runs (start),
 * GET /analysis-runs (polled while one is in progress), …/stop, …/resume,
 * …/reject-pending (undo: reject the run's pending suggestions).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { SUGGESTIONS_KEY } from './suggestions.js'

export const RUNS_KEY = ['analysis-runs']
const ACTIVE = new Set(['queued', 'waiting', 'running'])

export const RUN_STATUS_LABELS = {
  queued: 'En cola',
  waiting: 'Esperando archivos',
  running: 'Analizando',
  done: 'Terminado',
  stopped: 'Detenido',
  failed: 'Falló',
}

export const STOP_REASON_LABELS = {
  manual: 'Detenido por un administrador.',
  cost_limit: 'Se alcanzó el límite de costo.',
}

export function isRunActive(run) {
  return ACTIVE.has(run?.status)
}

export function useAnalysisRuns() {
  return useQuery({
    queryKey: RUNS_KEY,
    queryFn: ({ signal }) => api.get('/analysis-runs', { signal }),
    refetchInterval: (query) => ((query.state.data ?? []).some(isRunActive) ? 5000 : false),
  })
}

export function estimateRun(body) {
  return api.post('/analysis-runs/estimate', body)
}

function useRunMutation(fn) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: RUNS_KEY })
      queryClient.invalidateQueries({ queryKey: SUGGESTIONS_KEY })
    },
  })
}

export function useStartRun() {
  return useRunMutation((body) => api.post('/analysis-runs', body))
}

export function useStopRun() {
  return useRunMutation((id) => api.post(`/analysis-runs/${id}/stop`))
}

export function useResumeRun() {
  return useRunMutation(({ id, maxCostUsd }) =>
    api.post(`/analysis-runs/${id}/resume`, { max_cost_usd: maxCostUsd ?? null }),
  )
}

export function useRejectPending() {
  return useRunMutation((id) => api.post(`/analysis-runs/${id}/reject-pending`))
}

/** 12.3456 -> '12,35 USD'. */
export function formatUsd(value) {
  return `${new Intl.NumberFormat('es', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value ?? 0)} USD`
}

/** 'YYYY-MM-DD' -> '2 mar 2026' (a calendar date: no time zone involved). */
export function formatIsoDay(iso) {
  if (!iso) return ''
  const [y, m, d] = iso.split('-').map(Number)
  return new Intl.DateTimeFormat('es', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(y, m - 1, d)))
}
