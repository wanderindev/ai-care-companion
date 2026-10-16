import { useId } from 'react'
import { CalendarDaysIcon, XMarkIcon } from '@heroicons/react/20/solid'
import DoctorPicker from './DoctorPicker.jsx'
import { appointmentLabel } from '../../lib/bitacora.js'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none aria-[invalid=true]:border-red-500'
const labelClass = 'text-sm font-medium text-slate-700'
const optional = <span className="font-normal text-slate-500">(opcional)</span>

/**
 * "Próxima cita" section of the entry sheet: creates (or updates) the
 * appointment linked to the entry. Removing it cancels that appointment.
 */
export default function NextAppointmentField({
  value,
  onChange,
  entryDoctorId,
  existing,
  error,
}) {
  const ids = { date: useId(), time: useId(), location: useId(), notes: useId(), error: useId() }
  const set = (patch) => onChange({ ...value, ...patch })

  if (existing?.status === 'realizada') {
    return (
      <section className="rounded-2xl bg-slate-50 p-3 text-sm text-slate-700">
        <h3 className="font-medium text-slate-900">Próxima cita</h3>
        <p className="mt-1">
          {appointmentLabel(existing)} · esta cita ya se registró como realizada.
        </p>
      </section>
    )
  }

  if (!value.enabled) {
    return (
      <button
        type="button"
        onClick={() => set({ enabled: true })}
        className="flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl font-medium text-brand-700 ring-1 ring-brand-200 active:bg-brand-50"
      >
        <CalendarDaysIcon className="size-5" aria-hidden="true" />
        Agregar próxima cita
      </button>
    )
  }

  const doctorId = value.doctorTouched ? value.doctorId : entryDoctorId
  return (
    <section
      aria-labelledby={`${ids.date}-title`}
      className="space-y-3 rounded-2xl border border-brand-200 bg-brand-50/40 p-3"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 id={`${ids.date}-title`} className="flex items-center gap-2 font-medium">
          <CalendarDaysIcon className="size-5 text-brand-700" aria-hidden="true" />
          Próxima cita
        </h3>
        <button
          type="button"
          onClick={() => set({ enabled: false })}
          className="flex min-h-11 items-center gap-1 rounded-xl px-3 text-sm font-medium text-slate-600 active:bg-slate-100"
        >
          <XMarkIcon className="size-5" aria-hidden="true" />
          Quitar
        </button>
      </div>

      <div className="grid grid-cols-[3fr_2fr] gap-3">
        <div>
          <label htmlFor={ids.date} className={labelClass}>
            Fecha
          </label>
          <input
            id={ids.date}
            type="date"
            required
            value={value.date}
            onChange={(e) => set({ date: e.target.value })}
            aria-invalid={Boolean(error)}
            aria-describedby={error ? ids.error : undefined}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor={ids.time} className={labelClass}>
            Hora {optional}
          </label>
          <input
            id={ids.time}
            type="time"
            value={value.time}
            onChange={(e) => set({ time: e.target.value })}
            className={inputClass}
          />
        </div>
      </div>
      {error && (
        <p id={ids.error} role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
      {!value.time && <p className="text-xs text-slate-500">Sin hora, la cita es de todo el día.</p>}

      <DoctorPicker
        label="Médico de la cita"
        value={doctorId}
        onChange={(id) => set({ doctorId: id, doctorTouched: true })}
        fallback={existing?.doctor?.id === doctorId ? existing.doctor : undefined}
      />

      <div>
        <label htmlFor={ids.location} className={labelClass}>
          Lugar {optional}
        </label>
        <input
          id={ids.location}
          value={value.location}
          maxLength={300}
          onChange={(e) => set({ location: e.target.value })}
          placeholder="Ej.: Consultorio 4, piso 3"
          className={inputClass}
        />
      </div>

      <div>
        <label htmlFor={ids.notes} className={labelClass}>
          Notas {optional}
        </label>
        <input
          id={ids.notes}
          value={value.notes}
          maxLength={2000}
          onChange={(e) => set({ notes: e.target.value })}
          placeholder="Ej.: Llevar los exámenes"
          className={inputClass}
        />
      </div>
    </section>
  )
}
