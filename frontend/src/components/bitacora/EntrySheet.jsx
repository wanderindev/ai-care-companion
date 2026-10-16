import { useId, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { CheckCircleIcon } from '@heroicons/react/20/solid'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import DoctorPicker from './DoctorPicker.jsx'
import AttachmentsField from './AttachmentsField.jsx'
import NextAppointmentField from './NextAppointmentField.jsx'
import { usePatient } from '../../contexts/PatientContext.js'
import { useSaveLogEntry } from '../../lib/logEntries.js'
import { useCompleteAppointment } from '../../lib/appointments.js'
import {
  CHANNELS,
  ENTRY_TYPES,
  appointmentDate,
  appointmentLabel,
  appointmentTime,
  errorMessage,
  formatTime,
  initialNextAppointment,
  nextAppointmentPayload,
  todayISO,
  validateNextAppointment,
} from '../../lib/bitacora.js'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none aria-[invalid=true]:border-red-500'
const textareaClass = `${inputClass} py-2 leading-relaxed`
const labelClass = 'text-sm font-medium text-slate-700'
const optional = <span className="font-normal text-slate-500">(opcional)</span>

/** A new entry for the visit of an appointment ("Marcar como realizada"). */
function formFromAppointment(appt) {
  return {
    patientId: appt.patient_id,
    entryDate: appointmentDate(appt),
    entryTime: appointmentTime(appt),
    entryType: 'consulta_control',
    doctorId: appt.doctor_id,
    sourceLabel: '',
    channel: '',
    title: appt.reason ?? '',
    indications: '',
    familyNotes: '',
    markReviewed: false,
  }
}

function initialForm(entry, defaultPatientId, appointment) {
  if (appointment) return formFromAppointment(appointment)
  return {
    patientId: entry?.patient_id ?? defaultPatientId ?? null,
    entryDate: entry?.entry_date ?? todayISO(),
    entryTime: formatTime(entry?.entry_time),
    entryType: entry?.entry_type ?? 'consulta_control',
    doctorId: entry?.doctor_id ?? null,
    sourceLabel: entry?.source_label ?? '',
    channel: entry?.channel ?? '',
    title: entry?.title ?? '',
    indications: entry?.indications ?? '',
    familyNotes: entry?.family_notes ?? '',
    markReviewed: false,
  }
}

function validate(form, next) {
  const errors = {}
  const nextError = validateNextAppointment(next, form.entryDate)
  if (nextError) errors.next = nextError
  if (!form.patientId) errors.patientId = 'Elige para quién es la entrada.'
  if (!form.entryDate) errors.entryDate = 'Indica la fecha.'
  if (!form.doctorId && !form.sourceLabel.trim() && !form.title.trim()) {
    errors.header = 'Indica el médico, la fuente o un título.'
  }
  return errors
}

function fieldsOf(form) {
  const nullable = (text) => text.trim() || null
  return {
    patient_id: form.patientId,
    entry_date: form.entryDate,
    entry_time: form.entryTime || null,
    entry_type: form.entryType,
    doctor_id: form.doctorId,
    source_label: nullable(form.sourceLabel),
    channel: form.channel || null,
    title: nullable(form.title),
    indications: nullable(form.indications),
    family_notes: nullable(form.familyNotes),
  }
}

const doneIds = (files) => files.filter((f) => f.status === 'done').map((f) => f.attachment.id)

/**
 * A new entry gets every field. An edit sends only what changed in this
 * sheet, so it never overwrites what someone else saved meanwhile; above all
 * `attachment_ids` (which replaces the whole set) goes only when the user
 * added or removed a file here. The same goes for `next_appointment` (null
 * when the user removed it, which cancels the appointment); a `realizada` one
 * is never sent.
 */
function toPayload({ form, files, initial, initialFiles, entry, next, initialNext }) {
  const fields = fieldsOf(form)
  const nextBody = nextAppointmentPayload(next, form.doctorId)
  if (!entry) {
    const data = { ...fields, attachment_ids: doneIds(files) }
    if (nextBody) data.next_appointment = nextBody
    return data
  }
  const before = fieldsOf(initial)
  const data = Object.fromEntries(
    Object.entries(fields).filter(([key, value]) => value !== before[key]),
  )
  const ids = doneIds(files)
  if (ids.join() !== doneIds(initialFiles).join()) data.attachment_ids = ids
  if (entry.review_status === 'pending_review' && form.markReviewed) data.review_status = 'ok'
  const nextBefore = nextAppointmentPayload(initialNext, initial.doctorId)
  if (
    entry.next_appointment?.status !== 'realizada' &&
    JSON.stringify(nextBody) !== JSON.stringify(nextBefore)
  ) {
    data.next_appointment = nextBody
  }
  return data
}

/**
 * A suggestion's `next_appointment` body ({date, time?, doctor_id?, …}) as the
 * entry's `next_appointment` object the form starts from.
 */
function nextFromBody(body) {
  if (!body?.date) return undefined
  return {
    status: 'programada',
    starts_at: `${body.date}T${body.time ? body.time.slice(0, 5) : '00:00'}:00`,
    all_day: !body.time,
    doctor_id: body.doctor_id ?? null,
    location: body.location ?? null,
    notes: body.notes ?? null,
  }
}

/**
 * Create (no `entry`) or edit an entry in a bottom sheet / right drawer.
 * Closing with unsaved changes asks first.
 *
 * «Editar y aprobar» (Pendientes): `prefill` is a proposed body in the API
 * shape — a whole new entry, or (with `entry`) the proposed changes laid over
 * it — and `submit(data)` replaces the normal save (it receives the body this
 * sheet would POST/PATCH). `title` and `submitLabel` rename the sheet.
 *
 * With `appointment` (and no `entry`) it records that appointment's visit:
 * the form starts from the appointment (patient fixed, date, time, doctor,
 * reason as title) and saving goes through POST /appointments/{id}/complete,
 * which creates the entry and marks the appointment `realizada` in one
 * transaction. Closing without saving changes nothing.
 */
export default function EntrySheet({
  entry,
  appointment,
  prefill,
  submit: customSubmit,
  title,
  submitLabel = 'Guardar',
  onClose,
  onSaved,
}) {
  const { patients: allPatients, patient, selectedPatients } = usePatient()
  const completing = Boolean(appointment) && !entry
  // The visit is the appointment's patient's: the other one is not offered.
  const patients = completing
    ? allPatients.filter((p) => p.id === appointment.patient_id)
    : allPatients
  const defaultPatientId = patient !== 'ambos' ? selectedPatients[0]?.id : null
  const [initial] = useState(() =>
    initialForm(entry, defaultPatientId, completing ? appointment : null),
  )
  const [form, setForm] = useState(() =>
    prefill ? initialForm({ ...(entry ?? {}), ...prefill }, defaultPatientId, null) : initial,
  )
  const [initialFiles] = useState(() =>
    (entry?.attachments ?? []).map((a) => ({
      key: `a${a.id}`,
      status: 'done',
      attachment: a,
      name: a.filename,
    })),
  )
  const [files, setFiles] = useState(initialFiles)
  const [initialNext] = useState(() => initialNextAppointment(entry?.next_appointment))
  const [next, setNext] = useState(() => {
    if (!prefill || !('next_appointment' in prefill)) return initialNext
    const body = prefill.next_appointment
    const state = initialNextAppointment(nextFromBody(body))
    // Without a doctor_id the appointment follows the entry's doctor.
    return body && !('doctor_id' in body) ? { ...state, doctorTouched: false } : state
  })
  const [errors, setErrors] = useState({})
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const saveEntry = useSaveLogEntry()
  const complete = useCompleteAppointment()
  const custom = useMutation({ mutationFn: ({ data }) => customSubmit(data) })
  const save = customSubmit ? custom : completing ? complete : saveEntry
  const formId = useId()
  const ids = {
    date: useId(),
    time: useId(),
    type: useId(),
    source: useId(),
    channel: useId(),
    title: useId(),
    indications: useId(),
    notes: useId(),
    header: useId(),
  }

  const isPending = entry?.review_status === 'pending_review'
  const uploading = files.some((f) => f.status === 'uploading')
  const failed = files.filter((f) => f.status === 'error').length
  const dirty =
    JSON.stringify(form) !== JSON.stringify(initial) ||
    JSON.stringify(next) !== JSON.stringify(initialNext) ||
    files.map((f) => f.key).join() !== initialFiles.map((f) => f.key).join()

  const set = (field) => (value) => {
    setForm((f) => ({ ...f, [field]: value }))
    if (errors[field] || (errors.header && ['doctorId', 'sourceLabel', 'title'].includes(field))) {
      setErrors({})
    }
  }
  const onInput = (field) => (event) => set(field)(event.target.value)

  function requestClose() {
    if (save.isPending) return
    if (dirty) setConfirmDiscard(true)
    else onClose()
  }

  async function submit(event) {
    event.preventDefault()
    const found = validate(form, next)
    setErrors(found)
    if (Object.keys(found).length) {
      const first = document.querySelector(`#${CSS.escape(formId)} [aria-invalid="true"]`)
      first?.focus()
      return
    }
    try {
      const saved = await save.mutateAsync({
        id: completing ? appointment.id : entry?.id,
        data: toPayload({ form, files, initial, initialFiles, entry, next, initialNext }),
      })
      onSaved(saved)
    } catch {
      // Shown below the form from save.error.
    }
  }

  const footer = (
    <div className="space-y-2">
      {save.isError && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(save.error, 'No se pudo guardar la entrada. Inténtalo de nuevo.')}
        </p>
      )}
      {failed > 0 && !uploading && (
        <p className="text-sm text-slate-600">
          {failed === 1 ? 'Un archivo no se subió' : `${failed} archivos no se subieron`}: la
          entrada se guardará sin {failed === 1 ? 'él' : 'ellos'}.
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={requestClose}
          className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
        >
          Cancelar
        </button>
        <button
          type="submit"
          form={formId}
          disabled={save.isPending || uploading}
          className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
        >
          {save.isPending ? 'Guardando…' : uploading ? 'Subiendo archivos…' : submitLabel}
        </button>
      </div>
    </div>
  )

  return (
    <Sheet
      title={
        title ??
        (entry ? 'Editar entrada' : completing ? 'Registrar cita realizada' : 'Nueva entrada')
      }
      onClose={requestClose}
      footer={footer}
      wide
    >
      <form id={formId} onSubmit={submit} noValidate className="space-y-4">
        {completing && (
          <p className="rounded-2xl bg-brand-50 px-3 py-2 text-sm text-brand-900">
            Cita del {appointmentLabel(appointment)}. Al guardar, la entrada queda en la bitácora y
            la cita se marca como realizada.
          </p>
        )}
        {isPending && (
          <label className="flex min-h-12 cursor-pointer items-center gap-3 rounded-2xl border border-amber-300 bg-amber-50 px-3 py-2">
            <input
              type="checkbox"
              checked={form.markReviewed}
              onChange={(e) => set('markReviewed')(e.target.checked)}
              className="size-5 accent-brand-700"
            />
            <span className="flex-1">
              <span className="flex items-center gap-1 font-medium text-amber-950">
                <CheckCircleIcon className="size-5" aria-hidden="true" />
                Marcar como revisado
              </span>
              <span className="block text-sm text-amber-900">
                Se creó automáticamente y nadie la ha confirmado todavía.
              </span>
            </span>
          </label>
        )}

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
              value={form.entryDate}
              onChange={onInput('entryDate')}
              aria-invalid={Boolean(errors.entryDate)}
              className={inputClass}
            />
            {errors.entryDate && <p className="mt-1 text-sm text-red-700">{errors.entryDate}</p>}
          </div>
          <div>
            <label htmlFor={ids.time} className={labelClass}>
              Hora {optional}
            </label>
            <input
              id={ids.time}
              type="time"
              value={form.entryTime}
              onChange={onInput('entryTime')}
              className={inputClass}
            />
          </div>
        </div>

        <div>
          <label htmlFor={ids.type} className={labelClass}>
            Tipo
          </label>
          <select
            id={ids.type}
            value={form.entryType}
            onChange={onInput('entryType')}
            className={inputClass}
          >
            {ENTRY_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>

        <DoctorPicker
          value={form.doctorId}
          onChange={set('doctorId')}
          fallback={entry?.doctor ?? (completing ? appointment.doctor : undefined)}
        />

        <div>
          <label htmlFor={ids.source} className={labelClass}>
            Fuente {optional}
          </label>
          <input
            id={ids.source}
            value={form.sourceLabel}
            maxLength={200}
            onChange={onInput('sourceLabel')}
            placeholder="Ej.: Terapia respiratoria, Laboratorio"
            aria-describedby={`${ids.source}-hint`}
            className={inputClass}
          />
          <p id={`${ids.source}-hint`} className="mt-1 text-xs text-slate-500">
            Cuando no hay médico, o para anotar de dónde viene.
          </p>
        </div>

        <div>
          <label htmlFor={ids.channel} className={labelClass}>
            Canal {optional}
          </label>
          <select
            id={ids.channel}
            value={form.channel}
            onChange={onInput('channel')}
            className={inputClass}
          >
            <option value="">—</option>
            {CHANNELS.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor={ids.title} className={labelClass}>
            Motivo o diagnóstico {optional}
          </label>
          <input
            id={ids.title}
            value={form.title}
            maxLength={300}
            onChange={onInput('title')}
            placeholder="Ej.: Control de presión"
            aria-invalid={Boolean(errors.header)}
            aria-describedby={errors.header ? ids.header : undefined}
            className={inputClass}
          />
          {errors.header && (
            <p id={ids.header} role="alert" className="mt-1 text-sm text-red-700">
              {errors.header}
            </p>
          )}
        </div>

        <div>
          <label htmlFor={ids.indications} className={labelClass}>
            Indicaciones del médico {optional}
          </label>
          <textarea
            id={ids.indications}
            rows={5}
            value={form.indications}
            maxLength={50_000}
            onChange={onInput('indications')}
            className={textareaClass}
          />
        </div>

        <div>
          <label htmlFor={ids.notes} className={labelClass}>
            Notas de la familia {optional}
          </label>
          <textarea
            id={ids.notes}
            rows={3}
            value={form.familyNotes}
            maxLength={50_000}
            onChange={onInput('familyNotes')}
            className={textareaClass}
          />
          <p className="mt-1 text-xs text-slate-500">Puedes usar listas con «-» y **negritas**.</p>
        </div>

        <NextAppointmentField
          value={next}
          onChange={(value) => {
            setNext(value)
            // Clear only this section's error; the others stay until fixed.
            if (errors.next) setErrors((current) => ({ ...current, next: undefined }))
          }}
          entryDoctorId={form.doctorId}
          existing={entry?.next_appointment}
          error={errors.next}
        />

        <AttachmentsField items={files} onChange={setFiles} />
      </form>

      {confirmDiscard && (
        <ConfirmDialog
          title="¿Descartar los cambios?"
          message="Lo que escribiste en esta entrada no se guardará."
          confirmLabel="Descartar"
          cancelLabel="Seguir editando"
          danger
          onConfirm={onClose}
          onCancel={() => setConfirmDiscard(false)}
        />
      )}
    </Sheet>
  )
}
