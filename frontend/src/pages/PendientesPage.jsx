import { useMemo } from 'react'
import { InboxIcon } from '@heroicons/react/24/outline'
import SuggestionCard from '../components/suggestions/SuggestionCard.jsx'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import { usePatient } from '../contexts/PatientContext.js'
import { errorMessage } from '../lib/bitacora.js'
import { useDoctors } from '../lib/doctors.js'
import { useSuggestions } from '../lib/suggestions.js'

/**
 * «Pendientes» (DESIGN §6.7 step 5): what the WhatsApp analysis (or the
 * chat, or the doctor inference) proposes, one card per suggestion, for the selected patient(s); new
 * doctors show for both. Nothing is written until someone approves.
 */
export default function PendientesPage() {
  const { patients, patientIds } = usePatient()
  const query = useSuggestions(patientIds)
  const { data: doctors } = useDoctors()

  const ctx = useMemo(() => {
    const byId = new Map((doctors ?? []).map((d) => [d.id, d]))
    return {
      patients,
      doctorName: (id) => byId.get(id)?.name ?? 'Médico',
      doctorLabel: (id) => {
        const doctor = byId.get(id)
        if (!doctor) return 'Médico'
        return doctor.specialty ? `${doctor.name} · ${doctor.specialty}` : doctor.name
      },
      patientName: (id) => patients.find((p) => p.id === id)?.display_name ?? 'Paciente',
    }
  }, [doctors, patients])

  const items = query.data?.items ?? []

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900">Pendientes</h1>
        <p className="text-slate-600">
          Sugerencias a partir de los mensajes de WhatsApp, del chat o de la IA. Nada se guarda
          hasta que alguien las aprueba.
        </p>
      </header>

      {query.isPending ? (
        <CardSkeletons count={3} />
      ) : query.isError ? (
        <ErrorState
          message={errorMessage(query.error, 'No se pudieron cargar los pendientes.')}
          onRetry={() => query.refetch()}
        />
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
          <InboxIcon className="size-10 text-slate-400" aria-hidden="true" />
          <p className="font-semibold text-slate-800">No hay nada pendiente</p>
          <p className="text-slate-600">Las nuevas sugerencias aparecerán aquí.</p>
        </div>
      ) : (
        <ul className="space-y-4">
          {items.map((suggestion) => (
            <SuggestionCard key={suggestion.id} suggestion={suggestion} ctx={ctx} />
          ))}
        </ul>
      )}
    </div>
  )
}
