import { useId, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  ArrowUturnLeftIcon,
  CheckCircleIcon,
  NoSymbolIcon,
  TrashIcon,
} from '@heroicons/react/20/solid'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import DoctorPicker from '../bitacora/DoctorPicker.jsx'
import PdfButton from '../ui/PdfButton.jsx'
import { fetchPrevisitPdf } from '../../lib/summaries.js'
import { usePatient } from '../../contexts/PatientContext.js'
import {
  DURATIONS,
  canComplete,
  durationLabel,
  startsAtISO,
  useDeleteAppointment,
  useSaveAppointment,
} from '../../lib/appointments.js'
import {
  APPOINTMENT_STATUS_LABELS,
  appointmentDate,
  appointmentLabel,
  appointmentTime,
  errorMessage,
} from '../../lib/bitacora.js'
import { StatusBadge } from './AppointmentCard.jsx'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none aria-[invalid=true]:border-red-500'
const labelClass = 'text-sm font-medium text-slate-700'
const optional = <span className="font-normal text-slate-500">(opcional)</span>
const actionClass =
  'flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl font-medium ring-1 disabled:opacity-60'

function initialForm(appt, defaults) {
  return {
    patientId: appt?.patient_id ?? defaults.patientId ?? null,
    date: appt ? appointmentDate(appt) : (defaults.date ?? ''),
    time: appt ? appointmentTime(appt) : '',
    duration: appt?.duration_min ?? 30,
    doctorId: appt?.doctor_id ?? null,
    location: appt?.location ?? '',
    reason: appt?.reason ?? '',
    notes: appt?.notes ?? '',
  }
}

function fieldsOf(form) {
  const nullable = (text) => text.trim() || null
  return {
    patient_id: form.patientId,
    starts_at: form.date ? startsAtISO(form.date, form.time) : null,
    all_day: !form.time,
    duration_min: Number(form.duration),
    doctor_id: form.doctorId,
    location: nullable(form.location),
    reason: nullable(form.reason),
    notes: nullable(form.notes),
  }
}

/**
 * A new appointment gets every field; an edit sends only what changed here
 * (`starts_at` and `all_day` go together), so it never overwrites what
 * someone else saved meanwhile.
 */
function toPayload(form, initial, appt) {
  const fields = fieldsOf(form)
  if (!appt) return fields
  const before = fieldsOf(initial)
  const data = Object.fromEntries(
    Object.entries(fields).filter(([key, value]) => value !== before[key]),
  )
  if ('starts_at' in data || 'all_day' in data) {
    data.starts_at = fields.starts_at
    data.all_day = fields.all_day
  }
  return data
}

function validate(form) {
  const errors = {}
  if (!form.patientId) errors.patientId = 'Elige para quién es la cita.'
  if (!form.date) errors.date = 'Indica la fecha.'
  return errors
}

/**
 * Create (no `appt`) or edit an appointment in a bottom sheet / right drawer.
 * An existing one also offers «Marcar como realizada» (`onComplete`, from
 * today or earlier), «Cancelar cita» / «Volver a programar» and «Eliminar»
 * (for mistakes). Closing with unsaved changes asks first.
 *
 * «Editar y aprobar» (Pendientes): `prefill` is a proposed new appointment in
 * the API shape (`starts_at` on the Panamá clock) and `submit(data)` replaces
 * the normal save; `title` and `submitLabel` rename the sheet.
 */
export default function AppointmentSheet({
  appt,
  defaultDate,
  today,
  onClose,
  onSaved,
  onComplete,
  prefill,
  submit: customSubmit,
  title,
  submitLabel = 'Guardar',
}) {
  const { patients, patient, selectedPatients } = usePatient()
  const defaultPatientId = patient !== 'ambos' ? selectedPatients[0]?.id : null
  const [initial] = useState(() =>
    initialForm(appt ?? prefill, { patientId: defaultPatientId, date: defaultDate }),
  )
  const [form, setForm] = useState(initial)
  const [errors, setErrors] = useState({})
  const [confirm, setConfirm] = useState(null) // 'discard' | 'cancel' | 'delete'
  const saveAppt = useSaveAppointment()
  const custom = useMutation({ mutationFn: ({ data }) => customSubmit(data) })
  const save = customSubmit ? custom : saveAppt
  const status = useSaveAppointment()
  const remove = useDeleteAppointment()
  const formId = useId()
  const ids = {
    date: useId(),
    time: useId(),
    duration: useId(),
    location: useId(),
    reason: useId(),
    notes: useId(),
  }

  const busy = save.isPending || status.isPending || remove.isPending
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)
  const set = (field) => (value) => {
    setForm((f) => ({ ...f, [field]: value }))
    if (errors[field]) setErrors((e) => ({ ...e, [field]: undefined }))
  }
  const onInput = (field) => (event) => set(field)(event.target.value)

  function requestClose() {
    if (busy) return
    if (dirty) setConfirm('discard')
    else onClose()
  }

  async function submit(event) {
    event.preventDefault()
    const found = validate(form)
    setErrors(found)
    if (Object.keys(found).length) {
      document.querySelector(`#${CSS.escape(formId)} [aria-invalid="true"]`)?.focus()
      return
    }
    const data = toPayload(form, initial, appt)
    if (appt && !Object.keys(data).length) {
      onClose()
      return
    }
    try {
      onSaved(await save.mutateAsync({ id: appt?.id, data }), appt ? 'updated' : 'created')
    } catch {
      // Shown in the footer from save.error.
    }
  }

  async function setStatus(value) {
    try {
      const saved = await status.mutateAsync({ id: appt.id, data: { status: value } })
      setConfirm(null)
      onSaved(saved, value === 'cancelada' ? 'cancelled' : 'restored')
    } catch {
      // Shown in the dialog (cancel) or the footer (restore).
    }
  }

  async function destroy() {
    try {
      await remove.mutateAsync(appt.id)
      setConfirm(null)
      onSaved(null, 'deleted')
    } catch {
      // Shown in the dialog.
    }
  }

  const footerError = save.error ?? (confirm ? null : status.error)
  const footer = (
    <div className="space-y-2">
      {footerError && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(footerError, 'No se pudo guardar la cita. Inténtalo de nuevo.')}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={requestClose}
          className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
        >
          Cerrar
        </button>
        <button
          type="submit"
          form={formId}
          disabled={busy}
          className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
        >
          {save.isPending ? 'Guardando…' : submitLabel}
        </button>
      </div>
    </div>
  )

  return (
    <Sheet title={title ?? (appt ? 'Cita' : 'Nueva cita')} onClose={requestClose} footer={footer}>
      {appt && (
        <div className="mb-4 space-y-2 rounded-2xl bg-slate-50 p-3">
          <p className="flex flex-wrap items-center gap-2 text-sm text-slate-700">
            <span className="font-medium">{appointmentLabel(appt)}</span>
            <StatusBadge appt={appt} today={today} />
            {appt.status === 'programada' && appointmentDate(appt) >= today && (
              <span className="text-slate-500">{APPOINTMENT_STATUS_LABELS.programada}</span>
            )}
          </p>
          {appt.result_entry_id && (
            <Link
              to={`/bitacora/${appt.result_entry_id}`}
              className="inline-flex min-h-11 items-center font-medium text-brand-700 underline"
            >
              Ver la entrada de la bitácora
            </Link>
          )}
          {appt.created_from_entry_id && !appt.result_entry_id && (
            <Link
              to={`/bitacora/${appt.created_from_entry_id}`}
              className="inline-flex min-h-11 items-center text-sm font-medium text-brand-700 underline"
            >
              Ver la entrada donde se anotó
            </Link>
          )}
          {onComplete && canComplete(appt, today) && (
            <button
              type="button"
              disabled={busy || dirty}
              onClick={() => onComplete(appt)}
              className={`${actionClass} bg-brand-700 text-white ring-brand-700 active:bg-brand-800`}
            >
              <CheckCircleIcon className="size-5" aria-hidden="true" />
              Marcar como realizada
            </button>
          )}
          {onComplete && canComplete(appt, today) && dirty && (
            <p className="text-xs text-slate-500">Guarda los cambios antes de marcarla.</p>
          )}
          {appt.doctor_id && appt.status === 'programada' && (
            <PdfButton
              fetchFile={({ signal }) =>
                fetchPrevisitPdf(appt.patient_id, appt.doctor_id, { signal })
              }
              label="Resumen pre-consulta"
              ariaLabel="Resumen pre-consulta para esta cita (PDF)"
              shareTitle="Resumen pre-consulta"
              resetKey={`${appt.patient_id}:${appt.doctor_id}`}
            />
          )}
        </div>
      )}

      <form id={formId} onSubmit={submit} noValidate className="space-y-4">
        <fieldset aria-describedby={errors.patientId ? `${formId}-patient-error` : undefined}>
          <legend className={labelClass}>Paciente</legend>
          <div className="mt-1 grid grid-cols-2 gap-2">
            {patients.map((p) => {
              const checked = form.patientId === p.id
              return (
                <label
                  key={p.id}
                  className={`flex min-h-12 cursor-pointer items-center justify-center gap-2 rounded-2xl font-medium ring-1 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-brand-600 ${
                    checked
                      ? 'bg-brand-700 text-white ring-brand-700'
                      : 'bg-white text-slate-700 ring-slate-300'
                  }`}
                >
                  <input
                    type="radio"
                    name={`${formId}-patient`}
                    value={p.id}
                    checked={checked}
                    onChange={() => set('patientId')(p.id)}
                    aria-invalid={Boolean(errors.patientId)}
                    className="sr-only"
                  />
                  {checked && <CheckCircleIcon className="size-5" aria-hidden="true" />}
                  {p.display_name}
                </label>
              )
            })}
          </div>
          {errors.patientId && (
            <p id={`${formId}-patient-error`} className="mt-1 text-sm text-red-700">
              {errors.patientId}
            </p>
          )}
        </fieldset>

        <div className="grid grid-cols-[3fr_2fr] gap-3">
          <div>
            <label htmlFor={ids.date} className={labelClass}>
              Fecha
            </label>
            <input
              id={ids.date}
              type="date"
              required
              value={form.date}
              onChange={onInput('date')}
              aria-invalid={Boolean(errors.date)}
              className={inputClass}
            />
            {errors.date && <p className="mt-1 text-sm text-red-700">{errors.date}</p>}
          </div>
          <div>
            <label htmlFor={ids.time} className={labelClass}>
              Hora {optional}
            </label>
            <input
              id={ids.time}
              type="time"
              value={form.time}
              onChange={onInput('time')}
              className={inputClass}
            />
          </div>
        </div>
        {form.time ? (
          <div>
            <label htmlFor={ids.duration} className={labelClass}>
              Duración
            </label>
            <select
              id={ids.duration}
              value={form.duration}
              onChange={(e) => set('duration')(Number(e.target.value))}
              className={inputClass}
            >
              {[...new Set([...DURATIONS, Number(form.duration)])]
                .sort((a, b) => a - b)
                .map((m) => (
                  <option key={m} value={m}>
                    {durationLabel(m)}
                  </option>
                ))}
            </select>
          </div>
        ) : (
          <p className="text-xs text-slate-500">
            Sin hora, la cita es de todo el día. Hora de Panamá.
          </p>
        )}

        <DoctorPicker value={form.doctorId} onChange={set('doctorId')} fallback={appt?.doctor} />

        <div>
          <label htmlFor={ids.reason} className={labelClass}>
            Motivo {optional}
          </label>
          <input
            id={ids.reason}
            value={form.reason}
            maxLength={300}
            onChange={onInput('reason')}
            placeholder="Ej.: Control de presión"
            className={inputClass}
          />
        </div>

        <div>
          <label htmlFor={ids.location} className={labelClass}>
            Lugar {optional}
          </label>
          <input
            id={ids.location}
            value={form.location}
            maxLength={300}
            onChange={onInput('location')}
            placeholder="Ej.: Consultorio 4, piso 3"
            className={inputClass}
          />
        </div>

        <div>
          <label htmlFor={ids.notes} className={labelClass}>
            Notas {optional}
          </label>
          <textarea
            id={ids.notes}
            rows={3}
            value={form.notes}
            maxLength={20_000}
            onChange={onInput('notes')}
            placeholder="Ej.: Llevar los exámenes, ir en ayunas"
            className={`${inputClass} py-2 leading-relaxed`}
          />
        </div>
      </form>

      {appt && (
        <div className="mt-6 space-y-2 border-t border-slate-100 pt-4">
          {appt.status === 'programada' && (
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirm('cancel')}
              className={`${actionClass} text-slate-800 ring-slate-300 active:bg-slate-100`}
            >
              <NoSymbolIcon className="size-5" aria-hidden="true" />
              Cancelar cita
            </button>
          )}
          {appt.status === 'cancelada' && (
            <button
              type="button"
              disabled={busy}
              onClick={() => setStatus('programada')}
              className={`${actionClass} text-slate-800 ring-slate-300 active:bg-slate-100`}
            >
              <ArrowUturnLeftIcon className="size-5" aria-hidden="true" />
              {status.isPending ? 'Guardando…' : 'Volver a programar'}
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => setConfirm('delete')}
            className={`${actionClass} text-red-700 ring-red-200 active:bg-red-50`}
          >
            <TrashIcon className="size-5" aria-hidden="true" />
            Eliminar
          </button>
        </div>
      )}

      {confirm === 'discard' && (
        <ConfirmDialog
          title="¿Descartar los cambios?"
          message="Lo que escribiste en esta cita no se guardará."
          confirmLabel="Descartar"
          cancelLabel="Seguir editando"
          danger
          onConfirm={onClose}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm === 'cancel' && (
        <ConfirmDialog
          title="¿Cancelar esta cita?"
          message="Seguirá en el calendario, tachada como cancelada. Puedes volver a programarla."
          confirmLabel="Cancelar cita"
          cancelLabel="No, mantenerla"
          danger
          busy={status.isPending}
          error={status.isError ? errorMessage(status.error, 'No se pudo cancelar la cita.') : ''}
          onConfirm={() => setStatus('cancelada')}
          onCancel={() => {
            status.reset()
            setConfirm(null)
          }}
        />
      )}
      {confirm === 'delete' && (
        <ConfirmDialog
          title="¿Eliminar esta cita?"
          message="Úsalo solo si se anotó por error: desaparece del calendario. Si la cita no se va a dar, mejor cancélala."
          confirmLabel="Eliminar"
          cancelLabel="No eliminar"
          danger
          busy={remove.isPending}
          error={remove.isError ? errorMessage(remove.error, 'No se pudo eliminar la cita.') : ''}
          onConfirm={destroy}
          onCancel={() => {
            remove.reset()
            setConfirm(null)
          }}
        />
      )}
    </Sheet>
  )
}
