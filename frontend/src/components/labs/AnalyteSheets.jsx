import { useId, useMemo, useState } from 'react'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import { errorMessage } from '../../lib/bitacora.js'
import {
  SPECIMEN_LABELS,
  analyteMatches,
  specimenLabel,
  useAnalytes,
  useMergeAnalyte,
  useUpdateAnalyte,
} from '../../lib/analytes.js'

const NO_ANALYTES = []
const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none'

function Field({ label, children }) {
  return (
    <label className="block text-sm font-medium text-slate-700">
      {label}
      {children}
    </label>
  )
}

/**
 * «Confirmar»: the admin checks (and may fix) the name, unit, category and
 * specimen, then clears the review flag (PATCH /analytes/{id}).
 */
export function ConfirmAnalyteSheet({ analyte, onClose, onSaved }) {
  const update = useUpdateAnalyte()
  const [form, setForm] = useState({
    canonical_name: analyte.canonical_name,
    default_unit: analyte.default_unit ?? '',
    category: analyte.category ?? '',
    specimen: analyte.specimen,
  })
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  async function save() {
    const data = { needs_review: false }
    const name = form.canonical_name.trim()
    if (name !== analyte.canonical_name) data.canonical_name = name
    if (form.default_unit.trim() !== (analyte.default_unit ?? ''))
      data.default_unit = form.default_unit.trim()
    if (form.category.trim() !== (analyte.category ?? '')) data.category = form.category.trim()
    if (form.specimen !== analyte.specimen) data.specimen = form.specimen
    try {
      const saved = await update.mutateAsync({ id: analyte.id, data })
      onSaved(saved)
    } catch {
      // Shown below.
    }
  }

  return (
    <Sheet
      title="Confirmar examen"
      onClose={update.isPending ? () => {} : onClose}
      footer={
        <button
          type="button"
          onClick={save}
          disabled={update.isPending || !form.canonical_name.trim()}
          className="min-h-12 w-full rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800 disabled:opacity-50"
        >
          {update.isPending ? 'Guardando…' : 'Confirmar'}
        </button>
      }
    >
      <p className="text-slate-700">
        Revisa el nombre con el que aparecerá en las gráficas. Si cambias el nombre, el anterior se
        guarda como alias.
      </p>
      <div className="mt-4 space-y-4">
        <Field label="Nombre">
          <input
            value={form.canonical_name}
            onChange={set('canonical_name')}
            maxLength={200}
            className={inputClass}
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Unidad">
            <input
              value={form.default_unit}
              onChange={set('default_unit')}
              maxLength={50}
              className={inputClass}
            />
          </Field>
          <Field label="Muestra">
            <select value={form.specimen} onChange={set('specimen')} className={inputClass}>
              {Object.entries(SPECIMEN_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Categoría (opcional)">
          <input
            value={form.category}
            onChange={set('category')}
            maxLength={50}
            placeholder="Química, Hematología…"
            className={inputClass}
          />
        </Field>
      </div>
      {update.isError && (
        <p role="alert" className="mt-4 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(update.error, 'No se pudo guardar.')}
        </p>
      )}
    </Sheet>
  )
}

/**
 * «Combinar con…»: fold `source` (the flagged analyte) into an existing one
 * (POST /analytes/{target}/merge). Same specimen is offered first; another
 * specimen asks for an explicit confirmation.
 */
export function MergeAnalyteSheet({ source, onClose, onMerged }) {
  const { data: analytes = NO_ANALYTES, isPending, isError } = useAnalytes()
  const merge = useMergeAnalyte()
  const [query, setQuery] = useState('')
  const [target, setTarget] = useState(null)
  const searchId = useId()

  const candidates = useMemo(() => {
    const list = analytes.filter((a) => a.id !== source.id && analyteMatches(a, query))
    // Same specimen first, then by name (the API already sorts by name).
    return [
      ...list.filter((a) => a.specimen === source.specimen),
      ...list.filter((a) => a.specimen !== source.specimen),
    ]
  }, [analytes, source.id, source.specimen, query])

  const mismatch = target && target.specimen !== source.specimen

  async function confirm() {
    try {
      const merged = await merge.mutateAsync({
        targetId: target.id,
        sourceId: source.id,
        allowSpecimenMismatch: Boolean(mismatch),
      })
      onMerged(merged)
    } catch {
      // Shown in the dialog.
    }
  }

  return (
    <Sheet title="Combinar con…" onClose={merge.isPending ? () => {} : onClose}>
      <p className="text-slate-700">
        Elige el examen al que corresponde <strong>{source.canonical_name}</strong>. Sus resultados
        pasarán a ese examen y este se eliminará.
      </p>
      <label htmlFor={searchId} className="mt-4 block text-sm font-medium text-slate-700">
        Buscar examen
      </label>
      <input
        id={searchId}
        type="search"
        value={query}
        autoComplete="off"
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Nombre o alias…"
        className={inputClass}
      />
      {isError && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          No se pudo cargar la lista de exámenes.
        </p>
      )}
      <ul
        aria-label="Exámenes"
        className="mt-3 divide-y divide-slate-100 rounded-2xl border border-slate-200"
      >
        {!isPending && candidates.length === 0 && (
          <li className="px-3 py-3 text-sm text-slate-500">Ningún examen coincide.</li>
        )}
        {candidates.map((a) => (
          <li key={a.id}>
            <button
              type="button"
              onClick={() => {
                merge.reset()
                setTarget(a)
              }}
              className="flex min-h-12 w-full flex-col justify-center px-3 py-2 text-left active:bg-slate-100"
            >
              <span className="flex flex-wrap items-center gap-2 font-medium">
                {a.canonical_name}
                {a.specimen !== source.specimen && (
                  <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900">
                    {specimenLabel(a.specimen)}
                  </span>
                )}
                {a.needs_review && (
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-600">
                    Por revisar
                  </span>
                )}
              </span>
              <span className="text-sm text-slate-500">
                {[a.default_unit, a.aliases.join(', '), `${a.result_count} resultados`]
                  .filter(Boolean)
                  .join(' · ')}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {target && (
        <ConfirmDialog
          title={`¿Combinar «${source.canonical_name}» con «${target.canonical_name}»?`}
          message={
            <ul className="list-disc space-y-1 pl-5">
              {mismatch && (
                <li className="font-semibold text-red-800">
                  Las muestras son distintas: {specimenLabel(source.specimen)} y{' '}
                  {specimenLabel(target.specimen)}. Combínalos solo si de verdad son el mismo
                  examen.
                </li>
              )}
              <li>
                Los {source.result_count} resultados de <strong>{source.canonical_name}</strong>{' '}
                (de Papá y de Mamá) pasarán a <strong>{target.canonical_name}</strong>.
              </li>
              <li>
                «{source.canonical_name}» y sus alias se guardarán como alias de{' '}
                {target.canonical_name}.
              </li>
              <li>
                «{source.canonical_name}» se eliminará. <strong>No se puede deshacer</strong> desde
                la app.
              </li>
            </ul>
          }
          confirmLabel={merge.isPending ? 'Combinando…' : 'Combinar'}
          danger
          busy={merge.isPending}
          error={merge.isError ? errorMessage(merge.error, 'No se pudieron combinar.') : ''}
          onConfirm={confirm}
          onCancel={() => setTarget(null)}
        />
      )}
    </Sheet>
  )
}
