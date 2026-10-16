import { CheckCircleIcon, ExclamationTriangleIcon, MapPinIcon } from '@heroicons/react/20/solid'
import { appointmentTime } from '../../lib/bitacora.js'
import { canComplete, isUnconfirmed } from '../../lib/appointments.js'
import { patientColor } from '../../lib/patients.js'

/** Patient name chip in that patient's color (the name carries the meaning). */
export function PatientChip({ patient }) {
  if (!patient) return null
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${
        patientColor(patient.slug).chip
      }`}
    >
      <span
        className={`size-2 rounded-full ${patientColor(patient.slug).dot}`}
        aria-hidden="true"
      />
      {patient.display_name}
    </span>
  )
}

/** Status text (never color only). Nothing for an upcoming `programada`. */
export function StatusBadge({ appt, today }) {
  if (appt.status === 'cancelada') {
    return (
      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-700 ring-1 ring-slate-300 ring-inset">
        Cancelada
      </span>
    )
  }
  if (appt.status === 'realizada') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-800 ring-1 ring-emerald-200 ring-inset">
        <CheckCircleIcon className="size-3.5" aria-hidden="true" />
        Realizada
      </span>
    )
  }
  if (isUnconfirmed(appt, today)) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 ring-1 ring-amber-300 ring-inset">
        <ExclamationTriangleIcon className="size-3.5" aria-hidden="true" />
        Sin confirmar
      </span>
    )
  }
  return null
}

/**
 * One appointment in a day list. Tapping it opens the appointment sheet; a
 * past (or today's) `programada` one also offers «Marcar como realizada».
 */
export default function AppointmentCard({ appt, patient, showPatient, today, onOpen, onComplete }) {
  const cancelled = appt.status === 'cancelada'
  const time = appointmentTime(appt)
  const title = appt.reason || appt.doctor?.name || 'Cita médica'
  const showDoctor = appt.doctor && appt.reason

  return (
    <div
      className={`rounded-2xl border bg-white shadow-sm ${
        cancelled ? 'border-slate-200 opacity-70' : 'border-slate-200'
      }`}
    >
      <button
        type="button"
        onClick={() => onOpen(appt)}
        className="flex w-full gap-3 rounded-2xl p-4 text-left active:bg-slate-50"
      >
        <span className="w-14 shrink-0 pt-0.5 text-sm font-semibold text-slate-800">
          {time || <span className="text-xs font-medium text-slate-500">Todo el día</span>}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1.5">
            {showPatient && <PatientChip patient={patient} />}
            <StatusBadge appt={appt} today={today} />
          </span>
          <span
            className={`mt-1 block font-semibold text-slate-900 ${cancelled ? 'line-through' : ''}`}
          >
            {title}
          </span>
          {showDoctor && <span className="block text-sm text-slate-700">{appt.doctor.name}</span>}
          {appt.location && (
            <span className="mt-0.5 flex items-center gap-1 text-sm text-slate-500">
              <MapPinIcon className="size-4 shrink-0" aria-hidden="true" />
              <span className="min-w-0 truncate">{appt.location}</span>
            </span>
          )}
        </span>
      </button>
      {onComplete && canComplete(appt, today) && (
        <div className="border-t border-slate-100 px-2 py-1">
          <button
            type="button"
            onClick={() => onComplete(appt)}
            className="flex min-h-11 w-full items-center justify-center gap-2 rounded-xl font-medium text-brand-700 active:bg-brand-50"
          >
            <CheckCircleIcon className="size-5" aria-hidden="true" />
            Marcar como realizada
          </button>
        </div>
      )}
    </div>
  )
}
