import { Link } from 'react-router-dom'
import { ChevronRightIcon, InboxArrowDownIcon } from '@heroicons/react/24/outline'
import { KIND_LABELS, STATUS_LABELS } from '../../lib/suggestions.js'
import { patientColor, usePatientsQuery } from '../../lib/patients.js'

const STATUS_STYLES = {
  pending: 'bg-amber-50 text-amber-900 ring-amber-200',
  approved: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
  edited: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
  rejected: 'bg-slate-100 text-slate-600 ring-slate-200',
}

/**
 * A change the chat proposed (`propose_change`): it is not saved, it waits in
 * «Pendientes». The whole card links there. `suggestion` is `{id, kind,
 * patient_id, summary, status?}` (the live SSE event has no status: pending).
 */
export default function ProposalCard({ suggestion }) {
  const { data: patients } = usePatientsQuery()
  const patient = patients?.find((p) => p.id === suggestion.patient_id)
  const status = suggestion.status ?? 'pending'
  return (
    <Link
      to="/pendientes"
      className="flex min-h-12 w-full max-w-md items-center gap-3 rounded-2xl border border-brand-100 bg-white px-3 py-2.5 text-left active:bg-brand-50"
    >
      <InboxArrowDownIcon className="size-6 shrink-0 text-brand-700" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block text-xs font-medium text-brand-700">Propuesta para Pendientes</span>
        <span className="block text-sm font-semibold text-slate-900">
          {KIND_LABELS[suggestion.kind] ?? 'Cambio propuesto'}
        </span>
        {suggestion.summary && (
          <span className="block text-sm text-slate-700 [overflow-wrap:anywhere]">
            {suggestion.summary}
          </span>
        )}
        <span className="mt-1 flex flex-wrap gap-1.5">
          {patient && (
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${patientColor(patient.slug).chip}`}
            >
              {patient.display_name}
            </span>
          )}
          <span
            className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${STATUS_STYLES[status] ?? STATUS_STYLES.pending}`}
          >
            {status === 'pending' ? 'Sin guardar: revísala en Pendientes' : STATUS_LABELS[status]}
          </span>
        </span>
      </span>
      <ChevronRightIcon className="size-5 shrink-0 text-slate-400" aria-hidden="true" />
    </Link>
  )
}
