import { CalendarDaysIcon, ClockIcon } from '@heroicons/react/20/solid'
import { formatDate } from '../../lib/bitacora.js'
import { formatDays, formatQuantity } from '../../lib/meds.js'
import { LEDD_MARK, LEDD_MARK_AS_NEEDED } from '../../lib/ledd.js'

const MARKS = {
  counted: { symbol: LEDD_MARK, label: 'Cuenta para la LEDD' },
  as_needed: { symbol: LEDD_MARK_AS_NEEDED, label: 'Dosis si se da: no suma a la LEDD' },
}

/**
 * One medication as the printed sheet shows it: the quantity big on the left,
 * then name (bold) + strength, dose note, time / days / dates, the free-text
 * schedule and instructions, and who prescribed it. Shared by the read-only
 * list and the editor rows. `leddMark` ('counted' | 'as_needed') adds the
 * LEDD symbol after the name (with a text label, never color alone).
 */
export default function MedItemBody({ item, prescriberName, today, leddMark }) {
  const mark = MARKS[leddMark]
  const quantity = formatQuantity(item.quantity)
  const days = formatDays(item.days)
  const startsLater = item.start_date && today && item.start_date > today
  const when = [
    item.time && { icon: ClockIcon, text: item.time, label: 'Hora' },
    days && { icon: CalendarDaysIcon, text: days, label: 'Días' },
  ].filter(Boolean)
  const doctor = prescriberName ?? item.prescriber?.name

  return (
    <div className="flex gap-3">
      <div className="flex min-h-12 min-w-12 shrink-0 items-center justify-center self-start rounded-xl bg-brand-50 px-2 text-2xl leading-none font-bold text-brand-800 ring-1 ring-brand-100">
        <span className="sr-only">Cantidad: </span>
        {quantity || <span className="text-base font-normal text-slate-400">—</span>}
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-lg leading-snug">
          <span className="font-bold text-slate-900">{item.name}</span>
          {item.strength && <span className="text-slate-800"> {item.strength}</span>}
          {mark && (
            <span className="ml-1.5 text-base text-brand-700" title={mark.label}>
              <span aria-hidden="true">{mark.symbol}</span>
              <span className="sr-only"> ({mark.label})</span>
            </span>
          )}
        </p>
        {item.dose_note && <p className="text-slate-700">{item.dose_note}</p>}
        {when.length > 0 && (
          <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-slate-800">
            {when.map(({ icon: Icon, text, label }) => (
              <span key={label} className="inline-flex items-center gap-1 font-medium">
                <Icon className="size-4 text-slate-500" aria-hidden="true" />
                <span className="sr-only">{label}: </span>
                {text}
              </span>
            ))}
          </p>
        )}
        {(startsLater || item.end_date) && (
          <p className="mt-1 text-sm font-medium text-amber-800">
            {startsLater && `Empieza el ${formatDate(item.start_date)}`}
            {startsLater && item.end_date && ' · '}
            {item.end_date && `Hasta el ${formatDate(item.end_date)}`}
          </p>
        )}
        {item.schedule_note && (
          <p className="mt-1 whitespace-pre-line text-slate-800">{item.schedule_note}</p>
        )}
        {item.instructions && (
          <p className="mt-1 whitespace-pre-line text-slate-700 italic">{item.instructions}</p>
        )}
        {(doctor || item.reason) && (
          <p className="mt-1 text-sm text-slate-500">
            {doctor && `Indicado por ${doctor}`}
            {doctor && item.reason && ' · '}
            {item.reason && `Motivo: ${item.reason}`}
          </p>
        )}
      </div>
    </div>
  )
}
