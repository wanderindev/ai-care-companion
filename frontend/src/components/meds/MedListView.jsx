import { useId } from 'react'
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import Markdown from '../bitacora/Markdown.jsx'
import MedItemBody from './MedItemBody.jsx'
import LeddSummary from './LeddSummary.jsx'
import { formatDate } from '../../lib/bitacora.js'
import { panamaToday } from '../../lib/appointments.js'
import { effectiveDate } from '../../lib/meds.js'
import { leddMarks } from '../../lib/ledd.js'

/** «Desde …» and «Versión N · fecha · autor» + the latest change summary. */
export function VersionMeta({ list, authorName }) {
  const since = effectiveDate(list)
  const savedOn = list.changed_at ? formatDate(panamaToday(new Date(list.changed_at))) : ''
  return (
    <div className="rounded-2xl bg-white p-4 ring-1 ring-slate-200">
      {since && <p className="text-lg font-semibold text-slate-900">Desde {formatDate(since)}</p>}
      <p className="text-sm text-slate-600">
        {[`Versión ${list.version_no}`, savedOn, authorName].filter(Boolean).join(' · ')}
      </p>
      {list.change_summary && (
        <p className="mt-2 text-sm whitespace-pre-line text-slate-700">
          <span className="font-medium">Último cambio:</span> {list.change_summary}
        </p>
      )}
    </div>
  )
}

/**
 * The EVITAR block: red frame, a warning icon and the word «EVITAR», so it
 * never relies on color alone. `children` replaces the rows (the editor
 * passes its own, with buttons).
 */
export function AvoidBlock({ avoid, children }) {
  const headingId = useId()
  return (
    <section
      aria-labelledby={headingId}
      className="rounded-2xl border-2 border-red-300 bg-red-50 p-4"
    >
      <h3
        id={headingId}
        className="flex items-center gap-2 text-lg font-bold tracking-wide text-red-900"
      >
        <ExclamationTriangleIcon className="size-6" aria-hidden="true" />
        EVITAR
      </h3>
      {children ?? (
        <ul className="mt-2 space-y-2">
          {avoid.map((row) => (
            <li key={row.id} className="text-red-950">
              <span className="text-lg font-bold">{row.substance}</span>
              {row.reason && <span className="block text-red-900">{row.reason}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** Heading of one slot section, like the printed sheet's separators. */
export function SlotHeading({ children, id }) {
  return (
    <h3
      id={id}
      className="border-b-2 border-brand-700 pb-1 text-base font-bold tracking-wide text-brand-800 uppercase"
    >
      {children}
    </h3>
  )
}

/**
 * The current list, read-only: the non-empty slots in printed-sheet order,
 * the regimen note (markdown, tables allowed) and EVITAR. `showMeta={false}`
 * leaves out the «Desde» / version card (the version page has its own header).
 * `list.ledd` (ticket #133) adds the LEDD card and a mark on each item that
 * counts; `patientName` labels its «?» button when «Ambos» stacks two lists.
 */
export default function MedListView({ list, authorName, today, showMeta = true, patientName }) {
  const slots = list.slots.filter((group) => group.items.length > 0)
  const marks = leddMarks(list.ledd)
  return (
    <div className="space-y-5">
      {showMeta && <VersionMeta list={list} authorName={authorName} />}
      <LeddSummary ledd={list.ledd} patientName={patientName} />
      {slots.map((group) => (
        <section key={group.slot} aria-labelledby={`slot-${list.patient_id}-${group.slot}`}>
          <SlotHeading id={`slot-${list.patient_id}-${group.slot}`}>{group.label}</SlotHeading>
          <ul className="divide-y divide-slate-200">
            {group.items.map((item) => (
              <li key={item.id} className="py-3">
                <MedItemBody item={item} today={today} leddMark={marks.get(item.id)} />
              </li>
            ))}
          </ul>
        </section>
      ))}
      {slots.length === 0 && (
        <p className="rounded-2xl bg-white p-4 text-slate-600 ring-1 ring-slate-200">
          No hay medicamentos activos en esta versión de la lista.
        </p>
      )}
      {list.list_note && (
        <section className="rounded-2xl bg-white p-4 ring-1 ring-slate-200">
          <h3 className="mb-2 font-semibold text-slate-900">Notas de la lista</h3>
          <Markdown gfm className="text-slate-800">
            {list.list_note}
          </Markdown>
        </section>
      )}
      {list.avoid.length > 0 && <AvoidBlock avoid={list.avoid} />}
    </div>
  )
}
