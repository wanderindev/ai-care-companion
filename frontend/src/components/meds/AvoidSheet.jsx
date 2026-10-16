import { useId, useState } from 'react'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none aria-[invalid=true]:border-red-500'
const labelClass = 'text-sm font-medium text-slate-700'

/** Add or edit one EVITAR row of the working copy (saved with the whole list). */
export default function AvoidSheet({ row, isNew, onSave, onClose }) {
  const [initial] = useState(() => ({ substance: row.substance ?? '', reason: row.reason ?? '' }))
  const [form, setForm] = useState(initial)
  const [error, setError] = useState('')
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const formId = useId()
  const dirty = form.substance !== initial.substance || form.reason !== initial.reason

  function submit(event) {
    event.preventDefault()
    if (!form.substance.trim()) {
      setError('Escribe qué hay que evitar.')
      document.getElementById(`${formId}-substance`)?.focus()
      return
    }
    onSave({ ...row, substance: form.substance.trim(), reason: form.reason.trim() || null })
  }

  const footer = (
    <div className="flex gap-2">
      <button
        type="button"
        onClick={() => (dirty ? setConfirmDiscard(true) : onClose())}
        className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
      >
        Cancelar
      </button>
      <button
        type="submit"
        form={formId}
        className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800"
      >
        {isNew ? 'Agregar a EVITAR' : 'Aplicar cambios'}
      </button>
    </div>
  )

  return (
    <Sheet
      title={isNew ? 'Agregar a EVITAR' : 'Editar EVITAR'}
      onClose={() => (dirty ? setConfirmDiscard(true) : onClose())}
      footer={footer}
    >
      <form id={formId} onSubmit={submit} noValidate className="space-y-4">
        <div>
          <label htmlFor={`${formId}-substance`} className={labelClass}>
            Sustancia o medicamento
          </label>
          <input
            id={`${formId}-substance`}
            value={form.substance}
            maxLength={200}
            onChange={(e) => {
              setForm((f) => ({ ...f, substance: e.target.value }))
              setError('')
            }}
            placeholder="Ej.: Ibuprofeno"
            aria-invalid={Boolean(error)}
            aria-describedby={error ? `${formId}-error` : undefined}
            className={inputClass}
          />
          {error && (
            <p id={`${formId}-error`} className="mt-1 text-sm text-red-700">
              {error}
            </p>
          )}
        </div>
        <div>
          <label htmlFor={`${formId}-reason`} className={labelClass}>
            Motivo <span className="font-normal text-slate-500">(opcional)</span>
          </label>
          <textarea
            id={`${formId}-reason`}
            rows={3}
            value={form.reason}
            maxLength={2000}
            onChange={(e) => setForm((f) => ({ ...f, reason: e.target.value }))}
            placeholder="Ej.: le subió la presión"
            className={`${inputClass} py-2 leading-relaxed`}
          />
        </div>
      </form>
      {confirmDiscard && (
        <ConfirmDialog
          title="¿Descartar los cambios?"
          message="Lo que escribiste no se aplicará."
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
