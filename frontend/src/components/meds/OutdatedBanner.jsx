import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ExclamationTriangleIcon } from '@heroicons/react/20/solid'
import { errorMessage, formatDate } from '../../lib/bitacora.js'
import { EVENT_KIND_LABELS, useDismissMedCheck, useMedChecks } from '../../lib/meds.js'

const SHOWN = 3

/**
 * Out-of-date check (DESIGN §6.4, ticket #29): bitácora entries dated after
 * the list's «Desde» that mention medication changes. Never blocking: each
 * entry links to the bitácora and can be dismissed («Ya está reflejado», for
 * the whole family). Renders nothing when there is nothing to report.
 */
export default function OutdatedBanner({ patientId, versionNo }) {
  const checks = useMedChecks(patientId, { enabled: Boolean(versionNo) })
  const dismiss = useDismissMedCheck(patientId)
  const [expanded, setExpanded] = useState(false)
  const data = checks.data
  if (!versionNo || !data?.total) return null

  const entries = expanded ? data.entries : data.entries.slice(0, SHOWN)
  const heading =
    data.total === 1
      ? 'Hay 1 entrada de la bitácora posterior a la versión actual que menciona cambios de medicamentos.'
      : `Hay ${data.total} entradas de la bitácora posteriores a la versión actual que mencionan cambios de medicamentos.`

  return (
    <section
      aria-label="Entradas de la bitácora por revisar en la lista"
      className="mb-4 rounded-2xl border border-amber-300 bg-amber-50 p-3 text-amber-950"
    >
      <p className="flex gap-2 font-medium">
        <ExclamationTriangleIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
        {heading}
      </p>
      <p className="mt-1 text-sm text-amber-900">
        Revisa si la lista ya las incluye. Si es así, marca «Ya está reflejado».
      </p>
      <ul className="mt-3 space-y-2">
        {entries.map((entry) => (
          <li key={entry.id} className="rounded-xl bg-white p-3 ring-1 ring-amber-200">
            <p className="text-sm text-slate-600">{formatDate(entry.entry_date)}</p>
            {entry.title && <p className="font-medium text-slate-900">{entry.title}</p>}
            <ul className="mt-1 space-y-0.5 text-sm text-slate-800">
              {entry.events.map((event, i) => (
                <li key={i}>
                  <span className="font-medium">
                    {EVENT_KIND_LABELS[event.kind] ?? event.kind}:
                  </span>{' '}
                  {event.medication}
                  {event.detail ? ` — ${event.detail}` : ''}
                </li>
              ))}
            </ul>
            <div className="mt-2 flex flex-wrap gap-2">
              <Link
                to={`/bitacora/${entry.id}`}
                className="flex min-h-11 items-center rounded-xl px-3 font-medium text-brand-700 ring-1 ring-slate-300 active:bg-brand-50"
              >
                Ver entrada
              </Link>
              <button
                type="button"
                onClick={() => dismiss.mutate(entry.id)}
                disabled={dismiss.isPending}
                className="min-h-11 rounded-xl px-3 font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
              >
                Ya está reflejado
              </button>
            </div>
          </li>
        ))}
      </ul>
      {dismiss.isError && (
        <p role="alert" className="mt-2 text-sm text-red-800">
          {errorMessage(dismiss.error, 'No se pudo marcar la entrada. Inténtalo de nuevo.')}
        </p>
      )}
      {data.entries.length > SHOWN && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="mt-2 min-h-11 text-sm font-medium text-brand-700 underline"
        >
          {expanded ? 'Ver menos' : `Ver todas (${data.entries.length})`}
        </button>
      )}
      {data.total > data.entries.length && expanded && (
        <p className="mt-1 text-sm text-amber-900">
          Se muestran las {data.entries.length} más antiguas.
        </p>
      )}
    </section>
  )
}
