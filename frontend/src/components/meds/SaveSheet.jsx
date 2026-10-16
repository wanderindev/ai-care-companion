import { useId, useState } from 'react'
import Sheet from '../ui/Sheet.jsx'
import Markdown from '../bitacora/Markdown.jsx'
import MedDiff from './MedDiff.jsx'
import { ExclamationTriangleIcon } from '@heroicons/react/20/solid'
import { errorMessage } from '../../lib/bitacora.js'
import { leddChangeText } from '../../lib/ledd.js'
import {
  CHECKS_DISCLAIMER,
  LIST_NOTE_MAX,
  avoidWarningText,
  diffHasChanges,
} from '../../lib/meds.js'

const inputClass =
  'mt-1 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none'

/**
 * Confirmation step of a save: the list's «Desde» and «Nota de la lista»
 * (#135; `listFields` = the values the dry run was made with), the EVITAR
 * warnings of the dry run (never blocking), the auto-generated summary
 * (editable; it is stored with the version) and the readable diff. Changing
 * «Desde» (or leaving the note field) calls `onRefresh(fields)` so the parent
 * re-runs the dry run and passes a new `preview`. «Confirmar» calls
 * `onConfirm(summary, fields)` with the edited text (null when untouched: the
 * server then writes the summary for the final fields). Without `listFields`
 * (approving a suggested change, which keeps the list's «Desde» and note)
 * those two fields are not shown.
 */
export default function SaveSheet({
  preview,
  slots,
  listFields,
  busy,
  refreshing,
  error,
  onRefresh,
  onConfirm,
  onClose,
}) {
  const warnings = preview.warnings ?? []
  const editsList = Boolean(listFields)
  listFields ??= { effective_date: null, list_note: null }
  const [summary, setSummary] = useState(null) // null = the automatic one
  const [effectiveDate, setEffectiveDate] = useState(listFields.effective_date ?? '')
  const [note, setNote] = useState(listFields.list_note ?? '')
  const [sent, setSent] = useState(listFields) // the fields of the latest dry run asked for
  const baseId = useId()
  const shown = summary ?? preview.auto_summary
  const edited = summary !== null && summary.trim() !== preview.auto_summary.trim()
  const fields = () => ({ effective_date: effectiveDate || null, list_note: note.trim() || null })
  const same = (a, b) => a.effective_date === b.effective_date && a.list_note === b.list_note
  // The note is re-checked on blur: until then (or while a dry run is on its
  // way) the diff may be stale, so «Confirmar» stays enabled and the server
  // decides (the summary, if untouched, is written for the final fields).
  const canConfirm =
    diffHasChanges(preview.diff) || refreshing || (editsList && !same(fields(), sent))

  function refresh(next) {
    if (same(next, sent)) return
    setSent(next)
    onRefresh(next)
  }

  const footer = (
    <div className="space-y-2">
      {error && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(error, 'No se pudo guardar la lista. Inténtalo de nuevo.')}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onClose}
          disabled={busy}
          className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
        >
          Volver
        </button>
        <button
          type="button"
          onClick={() => onConfirm(edited && summary.trim() ? summary.trim() : null, fields())}
          disabled={busy || !canConfirm}
          className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
        >
          {busy ? 'Guardando…' : 'Confirmar y guardar'}
        </button>
      </div>
    </div>
  )

  return (
    <Sheet title="Revisar cambios" onClose={busy ? () => {} : onClose} footer={footer} wide>
      {warnings.length > 0 && (
        <div
          role="alert"
          className="mb-4 rounded-2xl border border-amber-300 bg-amber-50 p-3 text-amber-950"
        >
          <p className="flex items-center gap-2 font-semibold">
            <ExclamationTriangleIcon className="size-5 shrink-0" aria-hidden="true" />
            Revisa la lista EVITAR
          </p>
          <ul className="mt-2 space-y-1 text-sm">
            {warnings.map((w) => (
              <li key={`${w.item_index}-${w.substance}`}>
                {avoidWarningText(w.name, w.substance, w.reason)}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-sm text-amber-900">Puedes guardar igual. {CHECKS_DISCLAIMER}</p>
        </div>
      )}
      {preview.ledd_change && (
        <p
          role="status"
          className="mb-4 rounded-2xl bg-slate-50 px-3 py-2 font-semibold text-slate-900"
        >
          {leddChangeText(preview.ledd_change)}
          <span className="block text-sm font-normal text-slate-600">
            Dosis equivalente de levodopa (aproximación de referencia).
          </span>
        </p>
      )}
      <p className="text-slate-700">
        Al guardar se crea una nueva versión de la lista. Revisa el resumen; puedes corregirlo.
      </p>

      {editsList && (
        <>
        <label htmlFor={`${baseId}-desde`} className="mt-4 block text-sm font-medium text-slate-700">
          Desde
        </label>
        <input
          id={`${baseId}-desde`}
          type="date"
          value={effectiveDate}
          onChange={(e) => {
            setEffectiveDate(e.target.value)
            refresh({ ...fields(), effective_date: e.target.value || null })
          }}
          aria-describedby={`${baseId}-desde-help`}
          className={`${inputClass} min-h-12`}
        />
        <p id={`${baseId}-desde-help`} className="mt-1 text-sm text-slate-600">
          La fecha desde la que rige esta lista (sale en la hoja impresa).
        </p>

        <label htmlFor={`${baseId}-note`} className="mt-4 block text-sm font-medium text-slate-700">
          Nota de la lista
        </label>
        <textarea
          id={`${baseId}-note`}
          rows={5}
          value={note}
          maxLength={LIST_NOTE_MAX}
          onChange={(e) => setNote(e.target.value)}
          onBlur={() => refresh(fields())}
          aria-describedby={`${baseId}-note-help`}
          className={`${inputClass} py-2 font-mono text-sm leading-relaxed`}
        />
        <p id={`${baseId}-note-help`} className="mt-1 text-sm text-slate-600">
          Por ejemplo, la tabla de horarios. Revísala si cambió algún medicamento. Acepta tablas
          con «|».
        </p>
        {note.trim() && (
          <details className="mt-2 rounded-2xl bg-slate-50 px-3 py-2">
            <summary className="min-h-11 cursor-pointer content-center text-sm font-medium text-brand-700">
              Ver cómo queda la nota
            </summary>
            <Markdown gfm className="mt-2 text-slate-800">
              {note}
            </Markdown>
          </details>
        )}
        </>
      )}

      <label htmlFor={`${baseId}-summary`} className="mt-4 block text-sm font-medium text-slate-700">
        Resumen del cambio
      </label>
      <textarea
        id={`${baseId}-summary`}
        rows={4}
        value={shown}
        maxLength={5000}
        onChange={(e) => setSummary(e.target.value)}
        className={`${inputClass} py-2 leading-relaxed`}
      />
      {edited && (
        <button
          type="button"
          onClick={() => setSummary(null)}
          className="mt-1 min-h-11 text-sm font-medium text-brand-700 underline"
        >
          Usar el resumen automático
        </button>
      )}
      <h3 className="mt-4 text-sm font-medium text-slate-700">Detalle</h3>
      <div className="mt-1 rounded-2xl bg-slate-50 px-3 py-1" aria-busy={refreshing || undefined}>
        <MedDiff diff={preview.diff} slots={slots} />
      </div>
      {!canConfirm && (
        <p role="status" className="mt-2 text-sm text-slate-600">
          {editsList
            ? 'No hay cambios que guardar. Cambia la fecha «Desde» o la nota, o vuelve para editar la lista.'
            : 'Este cambio ya está en la lista actual: no hay nada que guardar.'}
        </p>
      )}
    </Sheet>
  )
}
