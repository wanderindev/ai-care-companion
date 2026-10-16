import { useState } from 'react'
import { ChevronUpIcon } from '@heroicons/react/20/solid'
import { CalendarDaysIcon } from '@heroicons/react/24/outline'
import {
  dayLabel,
  groupByDay,
  usePastAppointments,
  useUpcomingAppointments,
} from '../../lib/appointments.js'
import { errorMessage } from '../../lib/bitacora.js'
import { CardSkeletons, ErrorState } from '../bitacora/ListStates.jsx'
import DayList from './DayList.jsx'

const moreButton =
  'min-h-12 w-full rounded-2xl bg-white font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60'

/**
 * Upcoming appointments (today on) grouped by day; «Ver anteriores» loads
 * past ones above them, page by page (most recent first from the API, shown
 * in date order).
 */
export default function AgendaView({ patientIds, ready, today, onCreate, ...cardProps }) {
  const [showPast, setShowPast] = useState(false)
  const upcoming = useUpcomingAppointments(patientIds, today, { enabled: ready })
  const past = usePastAppointments(patientIds, today, { enabled: ready && showPast })

  if (!ready || upcoming.isPending) return <CardSkeletons count={3} />
  if (upcoming.isError) {
    return (
      <ErrorState
        message={errorMessage(upcoming.error, 'No se pudo cargar el calendario.')}
        onRetry={() => upcoming.refetch()}
      />
    )
  }

  const upcomingItems = upcoming.data.pages.flatMap((page) => page.items)
  const pastItems = showPast
    ? (past.data?.pages.flatMap((page) => page.items) ?? []).slice().reverse()
    : []
  const pastTotal = past.data?.pages[0]?.total
  const canLoadPast = !showPast || past.hasNextPage

  function loadPast() {
    if (!showPast) setShowPast(true)
    else past.fetchNextPage()
  }

  const loadingPast = (showPast && past.isPending) || past.isFetchingNextPage
  const renderGroups = (items, prefix) =>
    groupByDay(items).map((group) => (
      <DayList
        key={`${prefix}-${group.day}`}
        day={group.day}
        label={dayLabel(group.day, today)}
        items={group.items}
        sticky
        today={today}
        {...cardProps}
      />
    ))

  return (
    <div className="space-y-4">
      {canLoadPast && (
        <button type="button" onClick={loadPast} disabled={loadingPast} className={moreButton}>
          <span className="inline-flex items-center gap-2">
            <ChevronUpIcon className="size-5" aria-hidden="true" />
            {loadingPast ? 'Cargando…' : 'Ver anteriores'}
          </span>
        </button>
      )}
      {showPast && past.isError && (
        <p role="alert" className="text-center text-sm text-red-700">
          No se pudieron cargar las citas anteriores.
        </p>
      )}
      {showPast && pastTotal === 0 && (
        <p className="text-center text-sm text-slate-500">No hay citas anteriores.</p>
      )}

      {renderGroups(pastItems, 'past')}

      {showPast && pastItems.length > 0 && (
        <div className="flex items-center gap-3 text-xs font-semibold tracking-wide text-slate-500 uppercase">
          <span className="h-px flex-1 bg-slate-300" />
          Desde hoy
          <span className="h-px flex-1 bg-slate-300" />
        </div>
      )}

      {upcomingItems.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
          <CalendarDaysIcon className="mx-auto size-10 text-slate-400" aria-hidden="true" />
          <p className="mt-3 font-medium text-slate-800">No hay citas próximas.</p>
          <p className="mt-1 text-sm text-slate-500">
            Las citas que agregues aquí o como «Próxima cita» en la bitácora aparecerán en esta
            lista.
          </p>
          <button
            type="button"
            onClick={onCreate}
            className="mt-5 min-h-12 rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
          >
            Agregar una cita
          </button>
        </div>
      ) : (
        renderGroups(upcomingItems, 'next')
      )}

      {upcoming.hasNextPage && (
        <button
          type="button"
          onClick={() => upcoming.fetchNextPage()}
          disabled={upcoming.isFetchingNextPage}
          className={moreButton}
        >
          {upcoming.isFetchingNextPage ? 'Cargando…' : 'Ver más citas'}
        </button>
      )}
    </div>
  )
}
