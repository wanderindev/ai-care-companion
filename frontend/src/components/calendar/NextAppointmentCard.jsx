import { Link } from 'react-router-dom'
import { CalendarDaysIcon, ChevronRightIcon, MapPinIcon } from '@heroicons/react/20/solid'
import { usePatient } from '../../contexts/PatientContext.js'
import { dayLabel, panamaToday, useNextAppointment } from '../../lib/appointments.js'
import { appointmentDate, appointmentTime } from '../../lib/bitacora.js'
import { PatientChip } from './AppointmentCard.jsx'

/**
 * «Próxima cita» on top of the Bitácora: the next `programada` appointment
 * of the selected patient(s); tapping it opens it in the Calendario. Hidden
 * when there is none (or while loading / on error: it is a shortcut, not data
 * the page depends on).
 */
export default function NextAppointmentCard({ enabled }) {
  const { patient, patients, patientIds } = usePatient()
  const today = panamaToday()
  const { data: appt } = useNextAppointment(patientIds, today, { enabled })
  if (!appt) return null

  const time = appointmentTime(appt)
  const who = patients.find((p) => p.id === appt.patient_id)
  const title = appt.reason || appt.doctor?.name || 'Cita médica'
  return (
    <Link
      to={`/calendario?cita=${appt.id}`}
      className="mb-4 flex items-center gap-3 rounded-2xl border border-brand-100 bg-white p-3 shadow-sm active:bg-brand-50"
    >
      <span className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-brand-50 text-brand-700">
        <CalendarDaysIcon className="size-6" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <span className="font-semibold text-brand-800">Próxima cita</span>
          <span className="text-slate-700">
            {dayLabel(appointmentDate(appt), today)}
            {time ? ` · ${time}` : ''}
          </span>
          {patient === 'ambos' && <PatientChip patient={who} />}
        </span>
        <span className="block truncate font-medium text-slate-900">{title}</span>
        {appt.reason && appt.doctor && (
          <span className="block truncate text-sm text-slate-600">{appt.doctor.name}</span>
        )}
        {appt.location && (
          <span className="flex items-center gap-1 text-sm text-slate-500">
            <MapPinIcon className="size-4 shrink-0" aria-hidden="true" />
            <span className="truncate">{appt.location}</span>
          </span>
        )}
      </span>
      <ChevronRightIcon className="size-5 shrink-0 text-slate-400" aria-hidden="true" />
    </Link>
  )
}
