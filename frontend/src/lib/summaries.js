/**
 * Ready-made documents (docs/DESIGN.md §6.6, ticket #34): the «Resumen
 * pre-consulta» PDF and the «Tarjeta de emergencia» (PDF + screen data).
 *
 * Nothing here works offline: the service worker never caches /api
 * responses (they carry medical data), so the emergency screen needs a
 * connection like every other screen. «Descargar PDF» is the offline copy.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from './api.js'
import { fetchPdf } from './pdf.js'

export function fetchPrevisitPdf(patientId, doctorId, { signal } = {}) {
  return fetchPdf(`/patients/${patientId}/previsit.pdf?doctor_id=${doctorId}`, {
    signal,
    fallbackName: 'resumen-preconsulta.pdf',
  })
}

export function fetchEmergencyCardPdf(patientId, { signal } = {}) {
  return fetchPdf(`/patients/${patientId}/emergency-card.pdf`, {
    signal,
    fallbackName: 'tarjeta-emergencia.pdf',
  })
}

/** GET /patients/{id}/emergency-card: the card's data for `/emergencia`. */
export function useEmergencyCard(patientId) {
  return useQuery({
    queryKey: ['emergency-card', patientId],
    queryFn: ({ signal }) => api.get(`/patients/${patientId}/emergency-card`, { signal }),
    enabled: Boolean(patientId),
  })
}
