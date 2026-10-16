import { useState } from 'react'
import { useMe } from '../../lib/auth.js'
import { errorMessage } from '../../lib/bitacora.js'
import {
  LEDD_DISCLAIMER,
  formatDose,
  formatFactor,
  formatMg,
  useConfirmLeddSubstance,
  useRejectLeddSubstance,
} from '../../lib/ledd.js'

function names(list) {
  return list.filter(Boolean).join(', ')
}

/** An AI-classified name waiting for an admin: «Confirmar» (with its factor) or «No cuenta». */
function PendingRow({ row, canReview }) {
  const [factor, setFactor] = useState(row.proposed_factor ? String(row.proposed_factor) : '')
  const confirm = useConfirmLeddSubstance()
  const reject = useRejectLeddSubstance()
  const busy = confirm.isPending || reject.isPending
  const error = confirm.error || reject.error
  const value = Number(factor.replace(',', '.'))
  return (
    <li className="py-2">
      <p className="font-medium text-slate-900">
        {row.name} {row.strength && <span className="font-normal">{row.strength}</span>}
      </p>
      <p className="text-sm text-slate-600">
        Pendiente de confirmar: no suma.
        {row.proposed_factor != null &&
          ` Factor propuesto por IA: × ${formatDose(row.proposed_factor)}.`}
      </p>
      {canReview && (
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <label className="text-sm text-slate-700">
            Factor
            <input
              type="text"
              inputMode="decimal"
              value={factor}
              onChange={(e) => setFactor(e.target.value)}
              className="mt-1 block min-h-11 w-24 rounded-xl border border-slate-300 px-2"
            />
          </label>
          <button
            type="button"
            disabled={busy || !(value > 0)}
            onClick={() => confirm.mutate({ id: row.substance_id, factor: value })}
            className="min-h-11 rounded-xl bg-brand-700 px-3 text-sm font-semibold text-white active:bg-brand-800 disabled:opacity-60"
          >
            Confirmar
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => reject.mutate({ id: row.substance_id })}
            className="min-h-11 rounded-xl px-3 text-sm font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
          >
            No cuenta
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1 text-sm text-red-800">
          {errorMessage(error, 'No se pudo guardar la decisión.')}
        </p>
      )}
    </li>
  )
}

/**
 * What each medication contributes to the LEDD: drug, daily dose, factor,
 * mg and source; then the as-needed doses (never counted), what could not be
 * calculated and the names pending an admin's confirmation. `compact` (the
 * desktop hover popover) leaves out the notes and the admin buttons.
 */
export default function LeddBreakdown({ ledd, compact = false }) {
  const { data: me } = useMe()
  const contributions = ledd.contributions ?? []
  return (
    <div className="space-y-4 text-slate-800">
      {contributions.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-slate-700">Qué suma cada medicamento</h3>
          <ul className="mt-1 divide-y divide-slate-200">
            {contributions.map((c) => (
              <li key={c.substance} className="py-2">
                <p className="flex items-baseline justify-between gap-3">
                  <span className="font-medium text-slate-900">
                    {c.label}
                    <span className="font-normal text-slate-600"> ({names(c.names)})</span>
                  </span>
                  <span className="shrink-0 font-semibold">{formatMg(c.mg)} mg</span>
                </p>
                <p className="text-sm text-slate-600">
                  {c.kind === 'comt'
                    ? `${formatMg(c.base_mg)} mg de levodopa × ${formatDose(c.factor)}`
                    : c.kind === 'fixed'
                      ? formatFactor(c)
                      : `${formatDose(c.daily_dose_mg)} mg/día ${formatFactor(c)}`}
                  {' · '}
                  {c.source}
                </p>
                {!compact && c.note && <p className="text-sm text-slate-500">{c.note}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {ledd.as_needed?.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-slate-700">Si se da (no suma)</h3>
          <ul className="mt-1 space-y-1 text-sm">
            {ledd.as_needed.map((a) => (
              <li key={a.item_id}>
                {a.name} {a.strength}
                {a.max_mg != null ? `: + hasta ${formatMg(a.max_mg)} mg si se da` : ''}
              </li>
            ))}
          </ul>
        </section>
      )}
      {ledd.not_calculated?.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-slate-700">No calculado</h3>
          <ul className="mt-1 space-y-1 text-sm">
            {ledd.not_calculated.map((n) => (
              <li key={n.item_id}>
                {n.name} {n.strength}: {n.reason_text}
              </li>
            ))}
          </ul>
        </section>
      )}
      {ledd.pending?.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-slate-700">Pendiente de confirmar</h3>
          <ul className="mt-1 divide-y divide-slate-200">
            {ledd.pending.map((p) => (
              <PendingRow key={p.item_id} row={p} canReview={!compact && Boolean(me?.is_admin)} />
            ))}
          </ul>
        </section>
      )}
      <p className="text-xs text-slate-500">{LEDD_DISCLAIMER}</p>
    </div>
  )
}
