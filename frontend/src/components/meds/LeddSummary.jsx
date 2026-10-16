import { useState } from 'react'
import { QuestionMarkCircleIcon } from '@heroicons/react/24/outline'
import Sheet from '../ui/Sheet.jsx'
import LeddBreakdown from './LeddBreakdown.jsx'
import { useMediaQuery } from '../../lib/useMediaQuery.js'
import { LEDD_MARK, LEDD_MARK_AS_NEEDED, formatMg } from '../../lib/ledd.js'

/**
 * «LEDD ≈ N mg/día» (ticket #133) with a «?» next to the total: hovering it
 * (mouse) shows the breakdown in a popover; a tap or click opens it in a
 * bottom sheet (phones have no hover). Only for a list with any
 * dopaminergic item. `patientName` labels the button when «Ambos» stacks two lists.
 */
export default function LeddSummary({ ledd, patientName }) {
  const [open, setOpen] = useState(false)
  const [hover, setHover] = useState(false)
  const canHover = useMediaQuery('(hover: hover) and (pointer: fine)')
  if (!ledd?.has_dopaminergic) return null
  const hasAsNeeded = ledd.as_needed?.length > 0
  const label = patientName ? `Detalle de la LEDD de ${patientName}` : 'Detalle de la LEDD'

  return (
    <div className="rounded-2xl bg-white p-4 ring-1 ring-slate-200">
      <div className="relative flex items-center gap-2">
        <p className="text-lg font-semibold text-slate-900">
          LEDD ≈ {ledd.total_mg == null ? '—' : formatMg(ledd.total_mg)} mg/día
        </p>
        <span
          className="relative"
          onMouseEnter={() => canHover && setHover(true)}
          onMouseLeave={() => setHover(false)}
        >
          <button
            type="button"
            onClick={() => {
              setHover(false)
              setOpen(true)
            }}
            aria-label={label}
            aria-haspopup="dialog"
            className="flex size-11 items-center justify-center rounded-xl text-brand-700 active:bg-brand-50"
          >
            <QuestionMarkCircleIcon className="size-6" aria-hidden="true" />
          </button>
          {hover && !open && (
            <div
              aria-hidden="true"
              className="absolute top-full left-0 z-30 mt-1 max-h-[70vh] w-96 max-w-[80vw] overflow-y-auto rounded-2xl bg-white p-4 shadow-xl ring-1 ring-slate-200"
            >
              <LeddBreakdown ledd={ledd} compact />
            </div>
          )}
        </span>
      </div>
      <p className="text-sm text-slate-600">
        Dosis equivalente de levodopa: una aproximación de referencia para hablar con el neurólogo,
        no una indicación.
      </p>
      <p className="mt-1 text-sm text-slate-600">
        <span aria-hidden="true">{LEDD_MARK}</span> cuenta para la LEDD
        {hasAsNeeded && (
          <>
            {' · '}
            <span aria-hidden="true">{LEDD_MARK_AS_NEEDED}</span> si se da (no suma)
          </>
        )}
      </p>
      {ledd.pending?.length > 0 && (
        <p className="mt-1 text-sm font-medium text-amber-800">
          {ledd.pending.length === 1
            ? '1 medicamento pendiente de confirmar'
            : `${ledd.pending.length} medicamentos pendientes de confirmar`}
        </p>
      )}
      {open && (
        <Sheet title="LEDD: qué suma cada medicamento" onClose={() => setOpen(false)}>
          <p className="mb-3 text-lg font-semibold text-slate-900">
            LEDD ≈ {ledd.total_mg == null ? '—' : formatMg(ledd.total_mg)} mg/día
          </p>
          <LeddBreakdown ledd={ledd} />
        </Sheet>
      )}
    </div>
  )
}
