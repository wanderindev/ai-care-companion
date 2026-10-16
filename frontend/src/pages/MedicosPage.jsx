import { useId, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ChevronRightIcon, PhoneIcon, PlusIcon, UserGroupIcon } from '@heroicons/react/24/outline'
import { useMe } from '../lib/auth.js'
import { errorMessage } from '../lib/bitacora.js'
import { doctorMatches, telHref, useDoctors } from '../lib/doctors.js'
import DoctorInferencePanel from '../components/doctors/DoctorInferencePanel.jsx'
import DoctorSheet from '../components/doctors/DoctorSheet.jsx'
import InactiveBadge from '../components/doctors/InactiveBadge.jsx'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'

const NO_DOCTORS = []

function DoctorRow({ doctor }) {
  const details = [doctor.specialty, doctor.clinic].filter(Boolean).join(' · ')
  return (
    <li className="flex items-stretch rounded-2xl border border-slate-200 bg-white shadow-sm">
      <Link
        to={`/medicos/${doctor.id}`}
        className="flex min-h-16 min-w-0 flex-1 items-center gap-2 rounded-2xl py-3 pl-4 active:bg-slate-50"
      >
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-slate-900">{doctor.name}</span>
            {!doctor.is_active && <InactiveBadge />}
          </span>
          {details && <span className="block truncate text-sm text-slate-600">{details}</span>}
          {doctor.aliases.length > 0 && (
            <span className="block truncate text-sm text-slate-500">
              También: {doctor.aliases.join(', ')}
            </span>
          )}
        </span>
        {!doctor.phone && (
          <ChevronRightIcon className="mr-3 size-5 shrink-0 text-slate-400" aria-hidden="true" />
        )}
      </Link>
      {doctor.phone && (
        <a
          href={telHref(doctor.phone)}
          aria-label={`Llamar a ${doctor.name}: ${doctor.phone}`}
          className="flex min-w-14 shrink-0 flex-col items-center justify-center gap-0.5 rounded-r-2xl border-l border-slate-100 px-3 text-xs font-medium text-brand-700 active:bg-brand-50"
        >
          <PhoneIcon className="size-6" aria-hidden="true" />
          Llamar
        </a>
      )}
    </li>
  )
}

/**
 * Doctors directory: search (name or alias, accent-insensitive), add, open
 * detail. Admins also get «Completar médicos con IA» (#125).
 */
export default function MedicosPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const { data: me } = useMe()
  const query = searchParams.get('q') ?? ''
  const showInactive = searchParams.get('inactivos') === '1'
  const { data: doctors = NO_DOCTORS, isPending, isError, error, refetch } = useDoctors()
  const [creating, setCreating] = useState(false)
  const searchId = useId()

  const inactiveCount = doctors.filter((d) => !d.is_active).length
  const visible = useMemo(
    () => doctors.filter((d) => (showInactive || d.is_active) && doctorMatches(d, query)),
    [doctors, query, showInactive],
  )

  function setParam(key, value) {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next, { replace: true })
  }

  return (
    <section className="pb-16 lg:pb-0">
      <div className="mb-4 flex items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Médicos</h1>
          <p className="mt-0.5 text-sm text-slate-500" aria-live="polite">
            {isPending ? ' ' : visible.length === 1 ? '1 médico' : `${visible.length} médicos`}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="hidden min-h-11 items-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white hover:bg-brand-800 lg:flex"
        >
          <PlusIcon className="size-5" aria-hidden="true" />
          Nuevo médico
        </button>
      </div>

      {me?.is_admin && (
        <div className="mb-4">
          <DoctorInferencePanel />
        </div>
      )}

      <label htmlFor={searchId} className="sr-only">
        Buscar médico
      </label>
      <input
        id={searchId}
        type="search"
        value={query}
        autoComplete="off"
        onChange={(e) => setParam('q', e.target.value)}
        placeholder="Buscar por nombre o alias…"
        className="min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none"
      />
      {inactiveCount > 0 && (
        <label className="mt-2 flex min-h-11 w-fit cursor-pointer items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={(e) => setParam('inactivos', e.target.checked ? '1' : '')}
            className="size-5 accent-brand-700"
          />
          Mostrar inactivos ({inactiveCount})
        </label>
      )}

      <div className="mt-4">
        {isPending ? (
          <CardSkeletons count={5} />
        ) : isError ? (
          <ErrorState
            message={errorMessage(error, 'No se pudo cargar la lista de médicos.')}
            onRetry={() => refetch()}
          />
        ) : visible.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
            <UserGroupIcon className="mx-auto size-10 text-slate-400" aria-hidden="true" />
            <p className="mt-3 font-medium text-slate-800">
              {query ? 'Ningún médico coincide con la búsqueda.' : 'Todavía no hay médicos.'}
            </p>
            <button
              type="button"
              onClick={() => (query ? setParam('q', '') : setCreating(true))}
              className="mt-5 min-h-12 rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
            >
              {query ? 'Borrar búsqueda' : 'Agregar un médico'}
            </button>
          </div>
        ) : (
          <ul className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {visible.map((doctor) => (
              <DoctorRow key={doctor.id} doctor={doctor} />
            ))}
          </ul>
        )}
      </div>

      <button
        type="button"
        onClick={() => setCreating(true)}
        aria-label="Nuevo médico"
        className="fixed right-4 bottom-[calc(5rem+env(safe-area-inset-bottom))] z-10 flex size-14 items-center justify-center rounded-full bg-brand-700 text-white shadow-lg active:bg-brand-800 lg:hidden"
      >
        <PlusIcon className="size-7" aria-hidden="true" />
      </button>

      {creating && (
        <DoctorSheet
          onClose={() => setCreating(false)}
          onSaved={(doctor) => {
            setCreating(false)
            navigate(`/medicos/${doctor.id}`)
          }}
        />
      )}
    </section>
  )
}
