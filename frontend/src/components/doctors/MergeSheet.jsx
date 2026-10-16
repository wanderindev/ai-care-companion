import { useId, useMemo, useState } from 'react'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import InactiveBadge from './InactiveBadge.jsx'
import { errorMessage } from '../../lib/bitacora.js'
import { doctorMatches, useDoctors, useMergeDoctor } from '../../lib/doctors.js'

const NO_DOCTORS = []

/**
 * Admins only: pick the duplicate of `target` and fold it in
 * (POST /doctors/{id}/merge) after a confirmation that says what happens.
 */
export default function MergeSheet({ target, onClose, onMerged }) {
  const { data: doctors = NO_DOCTORS } = useDoctors()
  const [query, setQuery] = useState('')
  const [duplicate, setDuplicate] = useState(null)
  const merge = useMergeDoctor()
  const searchId = useId()

  const candidates = useMemo(
    () => doctors.filter((d) => d.id !== target.id && doctorMatches(d, query)),
    [doctors, target.id, query],
  )

  async function confirm() {
    try {
      const merged = await merge.mutateAsync({ targetId: target.id, sourceId: duplicate.id })
      onMerged(merged, duplicate)
    } catch {
      // Shown in the dialog.
    }
  }

  return (
    <Sheet title="Combinar con…" onClose={merge.isPending ? () => {} : onClose}>
      <p className="text-slate-700">
        Elige el registro duplicado de <strong>{target.name}</strong>. Sus entradas pasarán a este
        médico y el duplicado se eliminará.
      </p>
      <label htmlFor={searchId} className="mt-4 block text-sm font-medium text-slate-700">
        Buscar duplicado
      </label>
      <input
        id={searchId}
        type="search"
        value={query}
        autoComplete="off"
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Nombre o alias…"
        className="mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none"
      />
      <ul
        aria-label="Médicos"
        className="mt-3 divide-y divide-slate-100 rounded-2xl border border-slate-200"
      >
        {candidates.length === 0 && (
          <li className="px-3 py-3 text-sm text-slate-500">Ningún médico coincide.</li>
        )}
        {candidates.map((doctor) => (
          <li key={doctor.id}>
            <button
              type="button"
              onClick={() => {
                merge.reset()
                setDuplicate(doctor)
              }}
              className="flex min-h-12 w-full flex-col justify-center px-3 py-2 text-left active:bg-slate-100"
            >
              <span className="flex flex-wrap items-center gap-2 font-medium">
                {doctor.name}
                {!doctor.is_active && <InactiveBadge />}
              </span>
              {(doctor.specialty || doctor.aliases.length > 0) && (
                <span className="text-sm text-slate-500">
                  {[doctor.specialty, doctor.aliases.join(', ')].filter(Boolean).join(' · ')}
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>

      {duplicate && (
        <ConfirmDialog
          title={`¿Combinar «${duplicate.name}» con «${target.name}»?`}
          message={
            <ul className="list-disc space-y-1 pl-5">
              <li>
                Todas las entradas de la bitácora de <strong>{duplicate.name}</strong> (de Papá y
                de Mamá) pasarán a <strong>{target.name}</strong>.
              </li>
              <li>
                «{duplicate.name}» y sus alias se guardarán como alias de {target.name}; los datos
                de contacto que falten se copiarán.
              </li>
              <li>
                El registro duplicado se eliminará. <strong>No se puede deshacer</strong> desde la
                app.
              </li>
            </ul>
          }
          confirmLabel={merge.isPending ? 'Combinando…' : 'Combinar'}
          danger
          busy={merge.isPending}
          error={merge.isError ? errorMessage(merge.error, 'No se pudieron combinar.') : ''}
          onConfirm={confirm}
          onCancel={() => setDuplicate(null)}
        />
      )}
    </Sheet>
  )
}
