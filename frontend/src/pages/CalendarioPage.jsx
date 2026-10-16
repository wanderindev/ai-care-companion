import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  CheckCircleIcon,
  DevicePhoneMobileIcon,
  PlusIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { usePatientsQuery } from '../lib/patients.js'
import { panamaToday, useAppointment } from '../lib/appointments.js'
import { errorMessage } from '../lib/bitacora.js'
import { useIsDesktop } from '../lib/useMediaQuery.js'
import { ErrorState } from '../components/bitacora/ListStates.jsx'
import AgendaView from '../components/calendar/AgendaView.jsx'
import MonthView from '../components/calendar/MonthView.jsx'
import AppointmentSheet from '../components/calendar/AppointmentSheet.jsx'
import EntrySheet from '../components/bitacora/EntrySheet.jsx'

const VIEWS = [
  { value: 'agenda', label: 'Agenda' },
  { value: 'mes', label: 'Mes' },
]
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/

const NOTICES = {
  created: 'Cita guardada.',
  updated: 'Cambios guardados.',
  cancelled: 'Cita cancelada.',
  restored: 'Cita programada de nuevo.',
  deleted: 'Cita eliminada.',
  completed: 'Cita registrada como realizada.',
}

/**
 * Calendario (DESIGN §6.3). URL: `vista` (agenda | mes; phones default to
 * the agenda, desktop to the month), `mes` (YYYY-MM), `dia` (the tapped day)
 * and `cita` (an appointment to open, e.g. from the Bitácora's «Próxima
 * cita» card).
 */
export default function CalendarioPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const isDesktop = useIsDesktop()
  const { patient, patients, patientIds, isLoading: patientsLoading } = usePatient()
  const patientsQuery = usePatientsQuery()
  const today = panamaToday()

  const viewParam = searchParams.get('vista')
  const view =
    viewParam === 'mes' || viewParam === 'agenda' ? viewParam : isDesktop ? 'mes' : 'agenda'
  const monthParam = searchParams.get('mes')
  const month = MONTH_RE.test(monthParam ?? '') ? monthParam : today.slice(0, 7)
  const dayParam = searchParams.get('dia')
  const selectedDay = DAY_RE.test(dayParam ?? '') ? dayParam : null
  const linkedId = searchParams.get('cita')

  const [openAppt, setOpenAppt] = useState(null)
  const [creating, setCreating] = useState(false)
  const [completing, setCompleting] = useState(null)
  const [notice, setNotice] = useState(null)
  const linked = useAppointment(linkedId, { enabled: Boolean(linkedId) && !openAppt })

  const ready = !patientsLoading && !patientsQuery.isError
  const patientsById = useMemo(() => Object.fromEntries(patients.map((p) => [p.id, p])), [patients])
  const showPatient = patient === 'ambos'

  function updateParams(changes, { replace = false } = {}) {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(changes)) {
      if (value == null) next.delete(key)
      else next.set(key, value)
    }
    setSearchParams(next, { replace })
  }

  const sheetAppt = openAppt ?? (linkedId ? linked.data : null)
  function closeSheet() {
    setOpenAppt(null)
    if (linkedId) updateParams({ cita: null }, { replace: true })
  }

  const cardProps = {
    patientsById,
    showPatient,
    onOpen: (appt) => setOpenAppt(appt),
    onComplete: (appt) => setCompleting(appt),
  }

  return (
    <section className="pb-16 lg:pb-0">
      <div className="mb-4 flex items-end justify-between gap-3">
        <h1 className="text-2xl font-semibold">Calendario</h1>
        <Link
          to="/calendario/suscribir"
          className="ml-auto flex min-h-11 items-center gap-1.5 rounded-2xl px-3 text-sm font-medium text-brand-700 active:bg-brand-50"
        >
          <DevicePhoneMobileIcon className="size-5" aria-hidden="true" />
          En tu teléfono
        </Link>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="hidden min-h-11 items-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white hover:bg-brand-800 lg:flex"
        >
          <PlusIcon className="size-5" aria-hidden="true" />
          Nueva cita
        </button>
      </div>

      <div
        role="radiogroup"
        aria-label="Vista"
        className="mb-4 grid grid-cols-2 gap-1 rounded-2xl bg-slate-200/70 p-1 lg:max-w-xs"
      >
        {VIEWS.map((v) => {
          const checked = view === v.value
          return (
            <button
              key={v.value}
              type="button"
              role="radio"
              aria-checked={checked}
              onClick={() => updateParams({ vista: v.value }, { replace: true })}
              className={`min-h-11 rounded-xl font-medium ${
                checked ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'
              }`}
            >
              {v.label}
            </button>
          )
        })}
      </div>

      {notice && (
        <div
          role="status"
          className="mb-4 flex items-center gap-2 rounded-2xl border border-brand-100 bg-brand-50 py-1 pr-1 pl-3 text-brand-800"
        >
          <CheckCircleIcon className="size-5 shrink-0" aria-hidden="true" />
          <span className="flex-1 font-medium">{NOTICES[notice.kind]}</span>
          {notice.entryId && (
            <Link
              to={`/bitacora/${notice.entryId}`}
              className="flex min-h-11 items-center rounded-xl px-3 font-medium underline"
            >
              Ver entrada
            </Link>
          )}
          <button
            type="button"
            onClick={() => setNotice(null)}
            aria-label="Cerrar aviso"
            className="flex size-11 items-center justify-center rounded-xl active:bg-brand-100"
          >
            <XMarkIcon className="size-5" aria-hidden="true" />
          </button>
        </div>
      )}

      {linkedId && linked.isError && (
        <p role="alert" className="mb-4 rounded-2xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(linked.error, 'No se pudo abrir la cita.')}
        </p>
      )}

      {patientsQuery.isError ? (
        <ErrorState
          message={errorMessage(patientsQuery.error, 'No se pudo cargar la lista de pacientes.')}
          onRetry={() => patientsQuery.refetch()}
        />
      ) : view === 'mes' ? (
        <MonthView
          month={month}
          selectedDay={selectedDay}
          onMonth={(m) => updateParams({ mes: m, dia: null }, { replace: true })}
          onSelectDay={(day) =>
            updateParams(
              { mes: day.slice(0, 7), dia: day === selectedDay ? null : day },
              { replace: true },
            )
          }
          patientIds={patientIds}
          ready={ready}
          today={today}
          {...cardProps}
        />
      ) : (
        <AgendaView
          patientIds={patientIds}
          ready={ready}
          today={today}
          onCreate={() => setCreating(true)}
          {...cardProps}
        />
      )}

      {/* Quick add, above the bottom nav (phones). */}
      <button
        type="button"
        onClick={() => setCreating(true)}
        aria-label="Nueva cita"
        className="fixed right-4 bottom-[calc(5rem+env(safe-area-inset-bottom))] z-10 flex size-14 items-center justify-center rounded-full bg-brand-700 text-white shadow-lg active:bg-brand-800 lg:hidden"
      >
        <PlusIcon className="size-7" aria-hidden="true" />
      </button>

      {(creating || sheetAppt) && !completing && (
        <AppointmentSheet
          key={sheetAppt?.id ?? 'new'}
          appt={creating ? null : sheetAppt}
          defaultDate={view === 'mes' && selectedDay ? selectedDay : undefined}
          today={today}
          onClose={() => {
            setCreating(false)
            closeSheet()
          }}
          onSaved={(_saved, kind) => {
            setCreating(false)
            closeSheet()
            setNotice({ kind })
          }}
          onComplete={(appt) => setCompleting(appt)}
        />
      )}

      {completing && (
        <EntrySheet
          appointment={completing}
          onClose={() => setCompleting(null)}
          onSaved={(entry) => {
            setCompleting(null)
            setCreating(false)
            closeSheet()
            setNotice({ kind: 'completed', entryId: entry.id })
          }}
        />
      )}
    </section>
  )
}
