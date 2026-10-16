import { useState } from 'react'
import { CheckCircleIcon, ArrowsPointingInIcon } from '@heroicons/react/24/outline'
import { useMe } from '../lib/auth.js'
import { errorMessage } from '../lib/bitacora.js'
import { specimenLabel, useAnalytes } from '../lib/analytes.js'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import { ConfirmAnalyteSheet, MergeAnalyteSheet } from '../components/labs/AnalyteSheets.jsx'
import { TriagePanel, TriageProposal } from '../components/labs/AnalyteTriage.jsx'

const NO_ANALYTES = []

function AnalyteCard({ analyte, onConfirm, onMerge, onNotice }) {
  const details = [
    specimenLabel(analyte.specimen),
    analyte.default_unit,
    analyte.category,
    analyte.result_count === 1 ? '1 resultado' : `${analyte.result_count} resultados`,
  ].filter(Boolean)
  return (
    <article className="rounded-2xl border border-slate-200 bg-white p-4">
      <h2 className="text-lg font-semibold break-words">{analyte.canonical_name}</h2>
      <p className="mt-0.5 text-sm text-slate-600">{details.join(' · ')}</p>
      {analyte.aliases.length > 0 && (
        <p className="mt-1 text-sm text-slate-500">También: {analyte.aliases.join(', ')}</p>
      )}
      {analyte.examples.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-semibold text-slate-500 uppercase">Ejemplos</p>
          <ul className="mt-1 space-y-0.5 text-sm text-slate-700">
            {analyte.examples.map((e, i) => (
              <li key={i} className="break-words">
                {e.raw_name}: <span className="font-medium">{e.value_text ?? '—'}</span>
                {e.unit ? ` ${e.unit}` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
      <TriageProposal analyte={analyte} onNotice={onNotice} />
      <div className="mt-4 grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={onConfirm}
          className="flex min-h-12 items-center justify-center gap-2 rounded-2xl bg-brand-700 px-3 font-semibold text-white active:bg-brand-800"
        >
          <CheckCircleIcon className="size-5" aria-hidden="true" />
          Confirmar
        </button>
        <button
          type="button"
          onClick={onMerge}
          className="flex min-h-12 items-center justify-center gap-2 rounded-2xl px-3 font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100"
        >
          <ArrowsPointingInIcon className="size-5" aria-hidden="true" />
          Combinar con…
        </button>
      </div>
    </article>
  )
}

/**
 * Admin screen (header menu, «Revisar exámenes»): analytes the lab
 * extraction created from names it did not know. Confirm each one (fixing
 * its name, unit, category or specimen) or merge it into an existing one —
 * by hand, or by accepting the AI triage's proposal (#100, `TriagePanel`).
 */
export default function AnalyteReviewPage() {
  const { data: me } = useMe()
  const query = useAnalytes({ needsReview: true })
  const analytes = query.data ?? NO_ANALYTES
  const [confirming, setConfirming] = useState(null)
  const [merging, setMerging] = useState(null)
  const [notice, setNotice] = useState('')

  if (me && !me.is_admin) {
    return (
      <section>
        <h1 className="text-2xl font-semibold">Revisar exámenes</h1>
        <p className="mt-3 text-slate-600">Solo un administrador puede revisar los exámenes.</p>
      </section>
    )
  }

  return (
    <section>
      <h1 className="text-2xl font-semibold">Revisar exámenes</h1>
      <p className="mt-1 text-sm text-slate-600">
        La lectura automática no conocía estos nombres de examen: revisar es decidir si cada uno es
        un examen nuevo («Confirmar») o el mismo que uno que ya existe con otro nombre («Combinar
        con…»), y al combinarlos sus resultados pasan a ese examen, que guarda este nombre como
        alternativo, y sus gráficas quedan juntas.
      </p>
      <TriagePanel onNotice={setNotice} />
      {notice && (
        <p
          role="status"
          className="mt-4 rounded-xl bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
        >
          {notice}
        </p>
      )}
      <div className="mt-4">
        {query.isPending ? (
          <CardSkeletons />
        ) : query.isError ? (
          <ErrorState
            message={errorMessage(query.error, 'No se pudieron cargar los exámenes.')}
            onRetry={() => query.refetch()}
          />
        ) : analytes.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
            <CheckCircleIcon className="mx-auto size-10 text-emerald-500" aria-hidden="true" />
            <p className="mt-3 font-medium text-slate-800">No hay exámenes por revisar.</p>
          </div>
        ) : (
          <ul className="space-y-3">
            {analytes.map((analyte) => (
              <li key={analyte.id}>
                <AnalyteCard
                  analyte={analyte}
                  onConfirm={() => {
                    setNotice('')
                    setConfirming(analyte)
                  }}
                  onMerge={() => {
                    setNotice('')
                    setMerging(analyte)
                  }}
                  onNotice={setNotice}
                />
              </li>
            ))}
          </ul>
        )}
      </div>

      {confirming && (
        <ConfirmAnalyteSheet
          analyte={confirming}
          onClose={() => setConfirming(null)}
          onSaved={(saved) => {
            setConfirming(null)
            setNotice(`«${saved.canonical_name}» confirmado.`)
          }}
        />
      )}
      {merging && (
        <MergeAnalyteSheet
          source={merging}
          onClose={() => setMerging(null)}
          onMerged={(target) => {
            setNotice(`«${merging.canonical_name}» combinado con «${target.canonical_name}».`)
            setMerging(null)
          }}
        />
      )}
    </section>
  )
}
