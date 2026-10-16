/**
 * Calendar: appointment data hooks over /appointments (TanStack Query) and the
 * date helpers the calendar needs.
 *
 * Time zone rule: appointments live on the Panamá clock (UTC-5, no DST).
 * The API returns `starts_at` as '2026-04-14T14:00:00-05:00', so dates and
 * times are read off the string (`appointmentDate` / `appointmentTime` in
 * bitacora.js), and new values are sent with an explicit `-05:00`. Nothing
 * here converts through the device's time zone, so a sibling travelling
 * abroad sees the same day and hour as everyone in Panamá.
 *
 * Every write invalidates the whole `['appointments']` key; completing one
 * ("Marcar como realizada") also invalidates `['log-entries']`.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { LOG_ENTRIES_KEY } from './logEntries.js'
import { appointmentDate, formatLongDate } from './bitacora.js'

export const APPOINTMENTS_KEY = ['appointments']

const PANAMA_TZ = 'America/Panama'
const PANAMA_OFFSET = '-05:00'

// ── Dates (all YYYY-MM-DD strings, Panamá days) ─────────────────────────────

/** Today on the Panamá clock, whatever the device's time zone. */
export function panamaToday(now = new Date()) {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: PANAMA_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(now)
}

function toUTC(iso) {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d))
}

function fromUTC(date) {
  return date.toISOString().slice(0, 10)
}

export function addDays(iso, days) {
  const date = toUTC(iso)
  date.setUTCDate(date.getUTCDate() + days)
  return fromUTC(date)
}

/** '2026-10' → first and last day of that month. */
export function monthRange(month) {
  const [y, m] = month.split('-').map(Number)
  return {
    first: fromUTC(new Date(Date.UTC(y, m - 1, 1))),
    last: fromUTC(new Date(Date.UTC(y, m, 0))),
  }
}

export function addMonths(month, delta) {
  const [y, m] = month.split('-').map(Number)
  return fromUTC(new Date(Date.UTC(y, m - 1 + delta, 1))).slice(0, 7)
}

export function monthLabel(month) {
  const [y, m] = month.split('-').map(Number)
  const label = new Intl.DateTimeFormat('es', {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(y, m - 1, 1)))
  return label.charAt(0).toUpperCase() + label.slice(1)
}

/**
 * The month grid: whole weeks, Monday first (the Spanish / Panamá
 * convention), as arrays of 7 YYYY-MM-DD strings.
 */
export function monthWeeks(month) {
  const { first, last } = monthRange(month)
  // getUTCDay: 0 = Sunday … 6 = Saturday → days since Monday.
  const lead = (toUTC(first).getUTCDay() + 6) % 7
  const weeks = []
  let day = addDays(first, -lead)
  while (day <= last) {
    const week = []
    for (let i = 0; i < 7; i += 1) {
      week.push(day)
      day = addDays(day, 1)
    }
    weeks.push(week)
  }
  return weeks
}

export const WEEKDAYS = [
  { short: 'L', long: 'lunes' },
  { short: 'M', long: 'martes' },
  { short: 'X', long: 'miércoles' },
  { short: 'J', long: 'jueves' },
  { short: 'V', long: 'viernes' },
  { short: 'S', long: 'sábado' },
  { short: 'D', long: 'domingo' },
]

/** «Hoy», «Mañana», «Ayer» or 'Martes, 14 de abril de 2026'. */
export function dayLabel(iso, today) {
  if (iso === today) return 'Hoy'
  if (iso === addDays(today, 1)) return 'Mañana'
  if (iso === addDays(today, -1)) return 'Ayer'
  const long = formatLongDate(iso)
  return long.charAt(0).toUpperCase() + long.slice(1)
}

/** ISO `starts_at` with the explicit Panamá offset (all-day: midnight). */
export function startsAtISO(date, time) {
  return `${date}T${time || '00:00'}:00${PANAMA_OFFSET}`
}

/** A `programada` appointment whose day is over: nobody confirmed it happened. */
export function isUnconfirmed(appt, today) {
  return appt.status === 'programada' && appointmentDate(appt) < today
}

/**
 * Can it be recorded as done? Only `programada` ones from today or earlier:
 * the bitácora only records what already happened.
 */
export function canComplete(appt, today) {
  return appt.status === 'programada' && appointmentDate(appt) <= today
}

/** Group a list (already in display order) into [{ day, items }]. */
export function groupByDay(items) {
  const groups = []
  for (const item of items) {
    const day = appointmentDate(item)
    const last = groups[groups.length - 1]
    if (last?.day === day) last.items.push(item)
    else groups.push({ day, items: [item] })
  }
  return groups
}

export function durationLabel(minutes) {
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m ? `${h} h ${m} min` : `${h} h`
}

export const DURATIONS = [15, 30, 45, 60, 90, 120, 180]

// ── Queries ─────────────────────────────────────────────────────────────────

function listParams(patientIds, { dateFrom, dateTo, statuses, order, limit, offset }) {
  const params = new URLSearchParams()
  for (const id of patientIds) params.append('patient_id', String(id))
  for (const s of statuses ?? []) params.append('status', s)
  if (dateFrom) params.set('date_from', dateFrom)
  if (dateTo) params.set('date_to', dateTo)
  if (order) params.set('order', order)
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  return params
}

function useAppointmentsInfinite(name, patientIds, options, { enabled, pageSize }) {
  const base = listParams(patientIds, { ...options, limit: pageSize, offset: 0 }).toString()
  return useInfiniteQuery({
    queryKey: [...APPOINTMENTS_KEY, name, base],
    queryFn: ({ pageParam, signal }) =>
      api.get(
        `/appointments?${listParams(patientIds, { ...options, limit: pageSize, offset: pageParam })}`,
        { signal },
      ),
    initialPageParam: 0,
    getNextPageParam: (last) => {
      const next = last.offset + last.items.length
      return next < last.total && last.items.length > 0 ? next : undefined
    },
    enabled,
  })
}

/** Agenda: from today on, soonest first. */
export function useUpcomingAppointments(patientIds, today, { enabled = true } = {}) {
  return useAppointmentsInfinite(
    'upcoming',
    patientIds,
    { dateFrom: today, order: 'asc' },
    { enabled, pageSize: 50 },
  )
}

/** Agenda, "Ver anteriores": before today, most recent first. */
export function usePastAppointments(patientIds, today, { enabled = true } = {}) {
  return useAppointmentsInfinite(
    'past',
    patientIds,
    { dateTo: addDays(today, -1), order: 'desc' },
    { enabled, pageSize: 20 },
  )
}

/** Every appointment of a month (a family has a handful, never 500). */
export function useMonthAppointments(patientIds, month, { enabled = true } = {}) {
  const { first, last } = monthRange(month)
  const params = listParams(patientIds, {
    dateFrom: first,
    dateTo: last,
    order: 'asc',
    limit: 500,
    offset: 0,
  }).toString()
  return useQuery({
    queryKey: [...APPOINTMENTS_KEY, 'month', params],
    queryFn: ({ signal }) => api.get(`/appointments?${params}`, { signal }),
    enabled,
  })
}

/** The next `programada` appointment from today on (Bitácora's card). */
export function useNextAppointment(patientIds, today, { enabled = true } = {}) {
  const params = listParams(patientIds, {
    dateFrom: today,
    statuses: ['programada'],
    order: 'asc',
    limit: 1,
    offset: 0,
  }).toString()
  return useQuery({
    queryKey: [...APPOINTMENTS_KEY, 'next', params],
    queryFn: ({ signal }) => api.get(`/appointments?${params}`, { signal }),
    select: (page) => page.items[0] ?? null,
    enabled,
  })
}

export function useAppointment(id, { enabled = true } = {}) {
  return useQuery({
    queryKey: [...APPOINTMENTS_KEY, 'detail', String(id)],
    queryFn: ({ signal }) => api.get(`/appointments/${id}`, { signal }),
    retry: (count, error) => !(error?.status >= 400 && error?.status < 500) && count < 1,
    enabled: enabled && id != null,
  })
}

// ── Writes ──────────────────────────────────────────────────────────────────

function useAppointmentWrite(mutationFn, { entries = false } = {}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: APPOINTMENTS_KEY })
      // An entry's "Próxima cita" shows its appointment's status.
      if (entries) queryClient.invalidateQueries({ queryKey: LOG_ENTRIES_KEY })
    },
  })
}

/** Create (no `id`) or update (`id`, changed fields only). Resolves to the appointment. */
export function useSaveAppointment() {
  return useAppointmentWrite(
    ({ id, data }) =>
      id ? api.patch(`/appointments/${id}`, data) : api.post('/appointments', data),
    { entries: true },
  )
}

export function useDeleteAppointment() {
  return useAppointmentWrite((id) => api.delete(`/appointments/${id}`), { entries: true })
}

/**
 * "Marcar como realizada": POST /appointments/{id}/complete creates the
 * bitácora entry and links it (status realizada, result_entry_id) in one
 * transaction. Resolves to the new entry.
 */
export function useCompleteAppointment() {
  return useAppointmentWrite(({ id, data }) => api.post(`/appointments/${id}/complete`, data), {
    entries: true,
  })
}
