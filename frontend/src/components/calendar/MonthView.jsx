import { useMemo } from 'react'
import { ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/20/solid'
import {
  WEEKDAYS,
  addMonths,
  dayLabel,
  monthLabel,
  monthWeeks,
  useMonthAppointments,
} from '../../lib/appointments.js'
import { appointmentDate, errorMessage, formatLongDate } from '../../lib/bitacora.js'
import { patientColor } from '../../lib/patients.js'
import { ErrorState } from '../bitacora/ListStates.jsx'
import DayList from './DayList.jsx'

const navButton =
  'flex size-11 items-center justify-center rounded-xl text-slate-700 ring-1 ring-slate-300 active:bg-slate-100'

function countLabel(n) {
  return n === 1 ? '1 cita' : `${n} citas`
}

/**
 * Month grid (weeks start on Monday) with one dot per patient that has an
 * active (not cancelled) appointment that day; the day's full list shows
 * below the grid when a day is tapped.
 */
export default function MonthView({
  month,
  selectedDay,
  onMonth,
  onSelectDay,
  patientIds,
  ready,
  today,
  patientsById,
  showPatient,
  onOpen,
  onComplete,
}) {
  const query = useMonthAppointments(patientIds, month, { enabled: ready })
  const weeks = useMemo(() => monthWeeks(month), [month])

  const byDay = useMemo(() => {
    const map = {}
    for (const appt of query.data?.items ?? []) {
      const day = appointmentDate(appt)
      ;(map[day] ??= []).push(appt)
    }
    return map
  }, [query.data])

  // Per day: the patients with an active appointment, and how many.
  function dots(day) {
    const counts = new Map()
    for (const appt of byDay[day] ?? []) {
      if (appt.status === 'cancelada') continue
      counts.set(appt.patient_id, (counts.get(appt.patient_id) ?? 0) + 1)
    }
    return [...counts.entries()]
      .map(([id, count]) => ({ patient: patientsById[id], count }))
      .sort((a, b) => (a.patient?.id ?? 0) - (b.patient?.id ?? 0))
  }

  function cellLabel(day, dayDots) {
    const date = formatLongDate(day)
    const total = (byDay[day] ?? []).length
    if (!total) return `${date}, sin citas`
    const active = dayDots.reduce((n, d) => n + d.count, 0)
    const parts = dayDots.map((d) => `${d.patient?.display_name ?? 'Paciente'}: ${d.count}`)
    const cancelled = total - active
    return `${date}, ${countLabel(active)}${parts.length ? ` (${parts.join(', ')})` : ''}${
      cancelled ? `, ${cancelled === 1 ? '1 cancelada' : `${cancelled} canceladas`}` : ''
    }`
  }

  const selectedItems = selectedDay ? (byDay[selectedDay] ?? []) : []
  const inMonth = (day) => day.slice(0, 7) === month

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => onMonth(addMonths(month, -1))}
          aria-label="Mes anterior"
          className={navButton}
        >
          <ChevronLeftIcon className="size-5" aria-hidden="true" />
        </button>
        <h2 className="text-lg font-semibold" aria-live="polite">
          {monthLabel(month)}
        </h2>
        <button
          type="button"
          onClick={() => onMonth(addMonths(month, 1))}
          aria-label="Mes siguiente"
          className={navButton}
        >
          <ChevronRightIcon className="size-5" aria-hidden="true" />
        </button>
      </div>

      {query.isError ? (
        <ErrorState
          message={errorMessage(query.error, 'No se pudo cargar el calendario.')}
          onRetry={() => query.refetch()}
        />
      ) : (
        <div
          role="grid"
          aria-label={monthLabel(month)}
          aria-busy={!ready || query.isPending}
          className={`rounded-2xl border border-slate-200 bg-white p-1 ${
            !ready || query.isPending ? 'opacity-60' : ''
          }`}
        >
          <div role="row" className="grid grid-cols-7">
            {WEEKDAYS.map((d) => (
              <div
                key={d.long}
                role="columnheader"
                aria-label={d.long}
                className="py-1 text-center text-xs font-semibold text-slate-500"
              >
                {d.short}
              </div>
            ))}
          </div>
          {weeks.map((week) => (
            <div role="row" key={week[0]} className="grid grid-cols-7">
              {week.map((day) => {
                const dayDots = dots(day)
                const selected = day === selectedDay
                const isToday = day === today
                return (
                  <div role="gridcell" key={day} aria-selected={selected}>
                    <button
                      type="button"
                      onClick={() => onSelectDay(day)}
                      aria-label={cellLabel(day, dayDots)}
                      aria-current={isToday ? 'date' : undefined}
                      className={`flex h-14 w-full flex-col items-center justify-start gap-1 rounded-xl pt-1.5 sm:h-16 ${
                        selected ? 'bg-brand-700 text-white' : 'active:bg-slate-100'
                      } ${inMonth(day) ? '' : selected ? '' : 'text-slate-400'}`}
                    >
                      <span
                        className={`flex size-7 items-center justify-center rounded-full text-sm ${
                          isToday && !selected
                            ? 'font-bold text-brand-800 ring-2 ring-brand-600'
                            : ''
                        } ${isToday && selected ? 'font-bold' : ''}`}
                      >
                        {Number(day.slice(8))}
                      </span>
                      <span className="flex items-center gap-1" aria-hidden="true">
                        {dayDots.map(({ patient, count }) => (
                          <span key={patient?.id ?? 'x'} className="flex items-center">
                            <span
                              className={`size-2 rounded-full ${patientColor(patient?.slug).dot} ${
                                selected ? 'ring-1 ring-white' : ''
                              }`}
                            />
                            {count > 1 && (
                              <span className="ml-px text-[10px] leading-none font-semibold">
                                {count}
                              </span>
                            )}
                          </span>
                        ))}
                      </span>
                    </button>
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      )}

      {showPatient && (
        <ul className="flex flex-wrap gap-3 text-xs text-slate-600" aria-label="Leyenda">
          {Object.values(patientsById).map((p) => (
            <li key={p.id} className="flex items-center gap-1">
              <span
                className={`size-2.5 rounded-full ${patientColor(p.slug).dot}`}
                aria-hidden="true"
              />
              {p.display_name}
            </li>
          ))}
        </ul>
      )}

      {selectedDay &&
        (selectedItems.length ? (
          <DayList
            day={selectedDay}
            label={dayLabel(selectedDay, today)}
            items={selectedItems}
            patientsById={patientsById}
            showPatient={showPatient}
            today={today}
            onOpen={onOpen}
            onComplete={onComplete}
          />
        ) : (
          query.isSuccess && (
            <p className="rounded-2xl bg-white px-4 py-6 text-center text-sm text-slate-500 ring-1 ring-slate-200">
              {dayLabel(selectedDay, today)}: sin citas.
            </p>
          )
        ))}
    </div>
  )
}
