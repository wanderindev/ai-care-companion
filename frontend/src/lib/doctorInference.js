/**
 * «Completar médicos con IA» (#125, admins, on Médicos): Claude proposes the
 * missing doctor of a patient's bitácora entries; every proposal lands in
 * «Pendientes» (nothing changes until someone approves it).
 *
 * POST /doctor-inference/estimate (no cost) → POST /doctor-inference
 * (`{ job_id }`, progress via `useJob`).
 */
import { useMutation } from '@tanstack/react-query'
import { api } from './api.js'

export function estimateDoctorInference(body) {
  return api.post('/doctor-inference/estimate', body)
}

export function useStartDoctorInference() {
  return useMutation({
    mutationFn: (body) => api.post('/doctor-inference', body),
  })
}

/** Estimated cost in Spanish (`null` = unknown model prices). */
export function formatInferenceCost(cost) {
  if (cost == null) return 'desconocido'
  return cost < 0.01 ? 'menos de US$0.01' : `unos US$${cost.toFixed(2)}`
}
