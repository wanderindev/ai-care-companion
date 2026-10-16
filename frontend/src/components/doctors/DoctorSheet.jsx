import { useId, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { PlusIcon, XMarkIcon } from '@heroicons/react/20/solid'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import { errorMessage, normalizeText } from '../../lib/bitacora.js'
import { useCreateDoctor, useUpdateDoctor } from '../../lib/doctors.js'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none aria-[invalid=true]:border-red-500'
const labelClass = 'text-sm font-medium text-slate-700'
const optional = <span className="font-normal text-slate-500">(opcional)</span>

const TEXT_FIELDS = [
  { key: 'specialty', label: 'Especialidad', max: 100, placeholder: 'Cardiología' },
  { key: 'phone', label: 'Teléfono', max: 50, type: 'tel', placeholder: '6000-0000' },
  { key: 'email', label: 'Correo', max: 254, type: 'email', placeholder: 'consultorio@ejemplo.com' },
  { key: 'clinic', label: 'Clínica', max: 200, placeholder: 'Clínica / hospital' },
]

function initialForm(doctor) {
  return {
    name: doctor?.name ?? '',
    aliases: doctor?.aliases ?? [],
    specialty: doctor?.specialty ?? '',
    phone: doctor?.phone ?? '',
    email: doctor?.email ?? '',
    clinic: doctor?.clinic ?? '',
    address: doctor?.address ?? '',
    notes: doctor?.notes ?? '',
    isActive: doctor?.is_active ?? true,
  }
}

function fieldsOf(form) {
  const nullable = (text) => text.trim() || null
  return {
    name: form.name.trim(),
    aliases: form.aliases,
    specialty: nullable(form.specialty),
    phone: nullable(form.phone),
    email: nullable(form.email),
    clinic: nullable(form.clinic),
    address: nullable(form.address),
    notes: nullable(form.notes),
    is_active: form.isActive,
  }
}

/** A new doctor gets every field; an edit sends only what changed here. */
function toPayload(form, initial, doctor) {
  const fields = fieldsOf(form)
  if (!doctor) return fields
  const before = fieldsOf(initial)
  return Object.fromEntries(
    Object.entries(fields).filter(
      ([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key]),
    ),
  )
}

/** Editable chip list: other ways the family writes this doctor's name. */
function AliasEditor({ aliases, onChange, name }) {
  const [draft, setDraft] = useState('')
  const [notice, setNotice] = useState('')
  const inputRef = useRef(null)
  const inputId = useId()
  const hintId = useId()

  function add() {
    const alias = draft.trim()
    if (!alias) return
    const key = normalizeText(alias)
    if (key === normalizeText(name) || aliases.some((a) => normalizeText(a) === key)) {
      setNotice(`«${alias}» ya está incluido.`)
    } else {
      onChange([...aliases, alias])
      setNotice('')
    }
    setDraft('')
    inputRef.current?.focus()
  }

  function remove(index) {
    onChange(aliases.filter((_, i) => i !== index))
    inputRef.current?.focus()
  }

  return (
    <div>
      <label htmlFor={inputId} className={labelClass}>
        Otros nombres (alias) {optional}
      </label>
      <p id={hintId} className="text-sm text-slate-500">
        Cómo lo escribe la familia: «Dra. Pérez», «Anita»…
      </p>
      {aliases.length > 0 && (
        <ul aria-label="Alias" className="mt-2 flex flex-wrap gap-2">
          {aliases.map((alias, index) => (
            <li
              key={alias}
              className="flex min-h-11 items-center gap-1 rounded-full bg-slate-100 py-0.5 pr-0.5 pl-3 text-slate-800"
            >
              <span className="break-all">{alias}</span>
              <button
                type="button"
                onClick={() => remove(index)}
                aria-label={`Quitar alias ${alias}`}
                className="flex size-10 items-center justify-center rounded-full text-slate-500 active:bg-slate-200"
              >
                <XMarkIcon className="size-5" aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-1 flex gap-2">
        <input
          ref={inputRef}
          id={inputId}
          value={draft}
          maxLength={200}
          aria-describedby={hintId}
          onChange={(e) => {
            setDraft(e.target.value)
            setNotice('')
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            }
          }}
          placeholder="Agregar alias"
          className={inputClass}
        />
        <button
          type="button"
          onClick={add}
          disabled={!draft.trim() || aliases.length >= 50}
          className="mt-1 flex min-h-12 shrink-0 items-center gap-1 rounded-2xl px-4 font-medium text-brand-700 ring-1 ring-brand-600 active:bg-brand-50 disabled:opacity-50"
        >
          <PlusIcon className="size-5" aria-hidden="true" />
          Agregar
        </button>
      </div>
      <p aria-live="polite" className="mt-1 text-sm text-slate-600">
        {notice}
      </p>
    </div>
  )
}

/**
 * Create (no `doctor`) or edit a doctor in a bottom sheet / right drawer.
 * Closing with unsaved changes asks first.
 *
 * «Editar y aprobar» (Pendientes): `prefill` is a proposed new doctor in the
 * API shape and `submit(data)` replaces the normal create; `title` and
 * `submitLabel` rename the sheet.
 */
export default function DoctorSheet({
  doctor,
  prefill,
  submit: customSubmit,
  title,
  submitLabel = 'Guardar',
  onClose,
  onSaved,
}) {
  const [initial] = useState(() => initialForm(doctor ?? prefill))
  const [form, setForm] = useState(initial)
  const [nameError, setNameError] = useState('')
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const create = useCreateDoctor()
  const update = useUpdateDoctor()
  const custom = useMutation({ mutationFn: (data) => customSubmit(data) })
  const save = customSubmit ? custom : doctor ? update : create
  const formId = useId()
  const ids = { name: useId(), address: useId(), notes: useId(), active: useId() }
  const fieldIds = useId()

  const dirty = JSON.stringify(form) !== JSON.stringify(initial)
  const set = (field) => (value) => setForm((f) => ({ ...f, [field]: value }))
  const onInput = (field) => (event) => set(field)(event.target.value)

  function requestClose() {
    if (save.isPending) return
    if (dirty) setConfirmDiscard(true)
    else onClose()
  }

  async function submit(event) {
    event.preventDefault()
    if (!form.name.trim()) {
      setNameError('Escribe el nombre del médico.')
      document.getElementById(ids.name)?.focus()
      return
    }
    const data = toPayload(form, initial, doctor)
    if (doctor && Object.keys(data).length === 0) {
      onSaved(doctor)
      return
    }
    try {
      const saved = customSubmit
        ? await custom.mutateAsync(data)
        : doctor
          ? await update.mutateAsync({ id: doctor.id, data })
          : await create.mutateAsync(data)
      onSaved(saved)
    } catch {
      // Shown in the footer from save.error.
    }
  }

  const footer = (
    <div className="space-y-2">
      {save.isError && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(save.error, 'No se pudo guardar el médico. Revisa los datos.')}
          {save.error.status === 409 && ' Cambia el nombre o quita ese alias.'}
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
          disabled={save.isPending}
          className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
        >
          {save.isPending ? 'Guardando…' : submitLabel}
        </button>
      </div>
    </div>
  )

  return (
    <Sheet title={title ?? (doctor ? 'Editar médico' : 'Nuevo médico')} onClose={requestClose} footer={footer}>
      <form id={formId} onSubmit={submit} noValidate className="space-y-4">
        <div>
          <label htmlFor={ids.name} className={labelClass}>
            Nombre
          </label>
          <input
            id={ids.name}
            value={form.name}
            maxLength={200}
            required
            autoComplete="off"
            aria-invalid={Boolean(nameError)}
            aria-describedby={nameError ? `${ids.name}-error` : undefined}
            onChange={(e) => {
              set('name')(e.target.value)
              setNameError('')
            }}
            placeholder="Dra. Ana Gómez"
            className={inputClass}
          />
          {nameError && (
            <p id={`${ids.name}-error`} className="mt-1 text-sm text-red-700">
              {nameError}
            </p>
          )}
        </div>

        <AliasEditor aliases={form.aliases} onChange={set('aliases')} name={form.name} />

        {TEXT_FIELDS.map(({ key, label, max, type = 'text', placeholder }) => (
          <div key={key}>
            <label htmlFor={`${fieldIds}-${key}`} className={labelClass}>
              {label} {optional}
            </label>
            <input
              id={`${fieldIds}-${key}`}
              type={type}
              value={form[key]}
              maxLength={max}
              onChange={onInput(key)}
              placeholder={placeholder}
              className={inputClass}
            />
          </div>
        ))}

        <div>
          <label htmlFor={ids.address} className={labelClass}>
            Dirección {optional}
          </label>
          <textarea
            id={ids.address}
            rows={2}
            value={form.address}
            maxLength={2000}
            onChange={onInput('address')}
            className={`${inputClass} py-2`}
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
            maxLength={20000}
            onChange={onInput('notes')}
            placeholder="Horario, cómo pedir cita…"
            className={`${inputClass} py-2`}
          />
        </div>

        {doctor && (
          <label
            htmlFor={ids.active}
            className="flex min-h-12 cursor-pointer items-center gap-3 rounded-2xl border border-slate-200 px-3 py-2"
          >
            <input
              id={ids.active}
              type="checkbox"
              checked={form.isActive}
              onChange={(e) => set('isActive')(e.target.checked)}
              className="size-5 accent-brand-700"
            />
            <span className="flex-1">
              <span className="block font-medium text-slate-800">Activo</span>
              <span className="block text-sm text-slate-500">
                Los inactivos no aparecen al elegir médico en una entrada nueva; sus entradas se
                conservan.
              </span>
            </span>
          </label>
        )}
      </form>

      {confirmDiscard && (
        <ConfirmDialog
          title="¿Descartar los cambios?"
          message="Lo que escribiste en este formulario se perderá."
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
