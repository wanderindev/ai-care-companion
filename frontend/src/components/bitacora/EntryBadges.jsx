import { ExclamationTriangleIcon } from '@heroicons/react/20/solid'
import { entryTypeLabel } from '../../lib/bitacora.js'

// Color only reinforces the label text (never the only signal).
const TYPE_COLORS = {
  consulta_control: 'bg-sky-50 text-sky-800 ring-sky-200',
  consulta_enfermedad: 'bg-amber-50 text-amber-900 ring-amber-200',
  visita_domiciliaria: 'bg-violet-50 text-violet-800 ring-violet-200',
  teleconsulta: 'bg-indigo-50 text-indigo-800 ring-indigo-200',
  terapia: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
  vacuna: 'bg-teal-50 text-teal-800 ring-teal-200',
  procedimiento: 'bg-orange-50 text-orange-900 ring-orange-200',
  hospitalizacion: 'bg-red-50 text-red-800 ring-red-200',
  examen: 'bg-cyan-50 text-cyan-800 ring-cyan-200',
  observacion_familiar: 'bg-slate-100 text-slate-700 ring-slate-200',
  otro: 'bg-slate-100 text-slate-700 ring-slate-200',
}

export function TypeChip({ type }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
        TYPE_COLORS[type] ?? TYPE_COLORS.otro
      }`}
    >
      {entryTypeLabel(type)}
    </span>
  )
}

export function ReviewBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 ring-1 ring-amber-300 ring-inset">
      <ExclamationTriangleIcon className="size-3.5" aria-hidden="true" />
      Por revisar
    </span>
  )
}

export function PatientTag({ name }) {
  if (!name) return null
  return (
    <span className="inline-flex items-center rounded-full bg-brand-50 px-2 py-0.5 text-xs font-semibold text-brand-800 ring-1 ring-brand-100 ring-inset">
      {name}
    </span>
  )
}
