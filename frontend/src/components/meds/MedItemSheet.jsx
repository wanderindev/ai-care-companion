import { useId, useState } from 'react'
import { CheckIcon, ExclamationTriangleIcon } from '@heroicons/react/20/solid'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import DoctorPicker from '../bitacora/DoctorPicker.jsx'
import { CHECKS_DISCLAIMER, WEEKDAYS, itemWarnings } from '../../lib/meds.js'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none aria-[invalid=true]:border-red-500'
const labelClass = 'text-sm font-medium text-slate-700'
const optional = <span className="font-normal text-slate-500">(opcional)</span>
const QUICK_QUANTITIES = ['½', '¼', '1', '2']
const TEXT_FIELDS = [
  'name',
  'strength',
  'quantity',
  'dose_note',
  'time',
  'schedule_note',
  'instructions',
  'reason',
  'start_date',
  'end_date',
]

function initialForm(item) {
  const form = { slot: item.slot, days: item.days ?? [], prescriberId: item.prescriber_id }
  for (const field of TEXT_FIELDS) form[field] = item[field] ?? ''
  return form
}

/** The form back into a working-copy item (blank text → null, 7 days → daily). */
function toItem(f, base) {
  const out = { ...base, slot: f.slot, prescriber_id: f.prescriberId }
  for (const field of TEXT_FIELDS) out[field] = f[field].trim() || null
  out.days = f.days.length && f.days.length < 7 ? f.days : null
  if (f.prescriberId !== base.prescriber_id) out.prescriber = null
  return out
}

function validate(form) {
  const errors = {}
  if (!form.name.trim()) errors.name = 'Escribe el nombre del medicamento.'
  if (form.start_date && form.end_date && form.end_date < form.start_date) {
    errors.end_date = 'La fecha de fin no puede ser anterior a la de inicio.'
  }
  return errors
}

/**
 * Add or edit one medication of the working copy (nothing is saved here: the
 * whole list is saved from the editor). `item` is the row being edited or an
 * empty one; `slots` are the list's slots ({slot, label}) for the «Horario»
 * field, so moving an item to another slot is just changing it.
 */
export default function MedItemSheet({ item, isNew, slots, avoid, onSave, onClose }) {
  const [initial] = useState(() => initialForm(item))
  const [form, setForm] = useState(initial)
  const [errors, setErrors] = useState({})
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const formId = useId()
  const ids = Object.fromEntries([...TEXT_FIELDS, 'slot'].map((f) => [f, `${formId}-${f}`]))

  const dirty = JSON.stringify(form) !== JSON.stringify(initial)
  const set = (field, value) => {
    setForm((f) => ({ ...f, [field]: value }))
    if (errors[field]) setErrors((e) => ({ ...e, [field]: undefined }))
  }
  const onInput = (field) => (event) => set(field, event.target.value)
  const toggleDay = (day) =>
    set(
      'days',
      form.days.includes(day)
        ? form.days.filter((d) => d !== day)
        : [...form.days, day].sort((a, b) => a - b),
    )

  const draft = toItem(form, item)
  const warnings = itemWarnings(draft, avoid)

  function requestClose() {
    if (dirty) setConfirmDiscard(true)
    else onClose()
  }

  function submit(event) {
    event.preventDefault()
    const found = validate(form)
    setErrors(found)
    if (Object.keys(found).length) {
      document.querySelector(`#${CSS.escape(formId)} [aria-invalid="true"]`)?.focus()
      return
    }
    onSave(draft)
  }

  const textInput = (field, label, { placeholder, maxLength, required } = {}) => (
    <div>
      <label htmlFor={ids[field]} className={labelClass}>
        {label} {!required && optional}
      </label>
      <input
        id={ids[field]}
        value={form[field]}
        maxLength={maxLength}
        onChange={onInput(field)}
        placeholder={placeholder}
        aria-invalid={Boolean(errors[field])}
        aria-describedby={errors[field] ? `${ids[field]}-error` : undefined}
        className={inputClass}
      />
      {errors[field] && (
        <p id={`${ids[field]}-error`} className="mt-1 text-sm text-red-700">
          {errors[field]}
        </p>
      )}
    </div>
  )

  const footer = (
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
        className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800"
      >
        {isNew ? 'Agregar a la lista' : 'Aplicar cambios'}
      </button>
    </div>
  )

  return (
    <Sheet
      title={isNew ? 'Nuevo medicamento' : 'Editar medicamento'}
      onClose={requestClose}
      footer={footer}
    >
      <form id={formId} onSubmit={submit} noValidate className="space-y-4">
        {textInput('name', 'Nombre', {
          placeholder: 'Ej.: Losartán',
          maxLength: 200,
          required: true,
        })}
        {warnings.length > 0 && (
          <div role="status" className="rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-950">
            {warnings.map((w) => (
              <p key={w} className="flex gap-2">
                <ExclamationTriangleIcon className="size-5 shrink-0" aria-hidden="true" />
                {w}
              </p>
            ))}
            <p className="mt-1 text-amber-900">Puedes guardarlo igual. {CHECKS_DISCLAIMER}</p>
          </div>
        )}
        {textInput('strength', 'Concentración', { placeholder: 'Ej.: 50 mg', maxLength: 100 })}

        <div>
          <label htmlFor={ids.quantity} className={labelClass}>
            Cantidad {optional}
          </label>
          <input
            id={ids.quantity}
            value={form.quantity}
            maxLength={50}
            onChange={onInput('quantity')}
            placeholder="Ej.: 1, ½, 2 cápsulas"
            className={inputClass}
          />
          <div className="mt-2 flex gap-2" role="group" aria-label="Cantidades rápidas">
            {QUICK_QUANTITIES.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => set('quantity', q)}
                aria-pressed={form.quantity === q}
                className={`min-h-12 min-w-12 flex-1 rounded-2xl text-xl font-bold ring-1 ${
                  form.quantity === q
                    ? 'bg-brand-700 text-white ring-brand-700'
                    : 'bg-white text-slate-800 ring-slate-300 active:bg-slate-100'
                }`}
              >
                {q}
              </button>
            ))}
          </div>
        </div>

        {textInput('dose_note', 'Nota de dosis', {
          placeholder: 'Ej.: (125 mg en total)',
          maxLength: 200,
        })}

        <div className="grid grid-cols-[3fr_2fr] gap-3">
          <div>
            <label htmlFor={ids.slot} className={labelClass}>
              Horario
            </label>
            <select
              id={ids.slot}
              value={form.slot}
              onChange={onInput('slot')}
              className={inputClass}
            >
              {slots.map((s) => (
                <option key={s.slot} value={s.slot}>
                  {s.label}
                </option>
              ))}
            </select>
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

        <fieldset>
          <legend className={labelClass}>Días</legend>
          <p className="text-xs text-slate-500">
            {form.days.length === 0 || form.days.length === 7
              ? 'Todos los días. Marca días solo si no es diario.'
              : 'Solo los días marcados.'}
          </p>
          <div className="mt-1 grid grid-cols-7 gap-1">
            {WEEKDAYS.map((w) => {
              const on = form.days.includes(w.value)
              return (
                <button
                  key={w.value}
                  type="button"
                  aria-pressed={on}
                  aria-label={w.long}
                  onClick={() => toggleDay(w.value)}
                  className={`flex min-h-12 flex-col items-center justify-center rounded-xl text-sm font-medium ring-1 ${
                    on
                      ? 'bg-brand-700 text-white ring-brand-700'
                      : 'bg-white text-slate-700 ring-slate-300 active:bg-slate-100'
                  }`}
                >
                  {on && <CheckIcon className="size-4" aria-hidden="true" />}
                  {w.short}
                </button>
              )
            })}
          </div>
        </fieldset>

        <div>
          <label htmlFor={ids.schedule_note} className={labelClass}>
            Pauta especial {optional}
          </label>
          <textarea
            id={ids.schedule_note}
            rows={2}
            value={form.schedule_note}
            maxLength={10_000}
            onChange={onInput('schedule_note')}
            placeholder="Ej.: vie 25 sep; lun 28 sep…"
            className={`${inputClass} py-2 leading-relaxed`}
          />
        </div>
        <div>
          <label htmlFor={ids.instructions} className={labelClass}>
            Instrucciones {optional}
          </label>
          <textarea
            id={ids.instructions}
            rows={2}
            value={form.instructions}
            maxLength={10_000}
            onChange={onInput('instructions')}
            placeholder="Ej.: 30 min antes del desayuno"
            className={`${inputClass} py-2 leading-relaxed`}
          />
        </div>

        <DoctorPicker
          label="Indicado por"
          value={form.prescriberId}
          onChange={(id) => set('prescriberId', id)}
          fallback={item.prescriber}
        />
        {textInput('reason', 'Motivo', { placeholder: 'Ej.: presión alta', maxLength: 300 })}

        <div className="grid grid-cols-2 gap-3">
          {['start_date', 'end_date'].map((field) => (
            <div key={field}>
              <label htmlFor={ids[field]} className={labelClass}>
                {field === 'start_date' ? 'Desde' : 'Hasta'} {optional}
              </label>
              <input
                id={ids[field]}
                type="date"
                value={form[field]}
                onChange={onInput(field)}
                aria-invalid={Boolean(errors[field])}
                className={inputClass}
              />
            </div>
          ))}
        </div>
        {errors.end_date ? (
          <p className="-mt-2 text-sm text-red-700">{errors.end_date}</p>
        ) : (
          <p className="-mt-2 text-xs text-slate-500">
            Con fecha «Hasta», deja de aparecer en la lista después de ese día.
          </p>
        )}
      </form>

      {confirmDiscard && (
        <ConfirmDialog
          title="¿Descartar los cambios?"
          message="Lo que escribiste en este medicamento no se aplicará."
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
