import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { PlusIcon, UserIcon, XMarkIcon } from '@heroicons/react/20/solid'
import { errorMessage, normalizeText } from '../../lib/bitacora.js'
import { matchDoctors, useCreateDoctor, useDoctors } from '../../lib/doctors.js'

const MAX_RESULTS = 8
const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none'

/** Mini-form: name + optional specialty. A 409 offers the existing doctor. */
function AddDoctorForm({ initialName, onCreated, onCancel }) {
  const [name, setName] = useState(initialName)
  const [specialty, setSpecialty] = useState('')
  const [existing, setExisting] = useState(null)
  const create = useCreateDoctor()
  const nameId = useId()
  const specialtyId = useId()

  async function submit() {
    setExisting(null)
    if (!name.trim()) return
    try {
      const doctor = await create.mutateAsync({ name: name.trim(), specialty })
      onCreated(doctor)
    } catch (error) {
      if (error.status === 409) {
        const matches = await matchDoctors(name.trim()).catch(() => [])
        setExisting(matches[0] ?? null)
      }
    }
  }

  // Not a <form>: this sits inside the entry form, and forms can't nest.
  return (
    <div className="mt-2 rounded-2xl border border-brand-100 bg-brand-50/60 p-3">
      <p className="font-medium text-slate-800">Agregar doctor</p>
      <label htmlFor={nameId} className="mt-2 block text-sm font-medium text-slate-700">
        Nombre
      </label>
      <input
        id={nameId}
        autoFocus
        value={name}
        maxLength={200}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            submit()
          }
        }}
        placeholder="Dra. Ana Gómez"
        className={inputClass}
      />
      <label htmlFor={specialtyId} className="mt-2 block text-sm font-medium text-slate-700">
        Especialidad <span className="font-normal text-slate-500">(opcional)</span>
      </label>
      <input
        id={specialtyId}
        value={specialty}
        maxLength={100}
        onChange={(e) => setSpecialty(e.target.value)}
        placeholder="Cardiología"
        className={inputClass}
      />
      {create.isError && (
        <div role="alert" className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {create.error.status === 409 ? (
            existing ? (
              <>
                <p>
                  Ya existe <strong>{existing.name}</strong>
                  {existing.specialty ? ` (${existing.specialty})` : ''}.
                </p>
                <button
                  type="button"
                  onClick={() => onCreated(existing)}
                  className="mt-2 min-h-11 rounded-xl bg-white px-3 font-medium text-brand-800 ring-1 ring-brand-600"
                >
                  Usar este médico
                </button>
              </>
            ) : (
              'Ya existe un médico con ese nombre o alias. Búscalo en la lista.'
            )
          ) : (
            errorMessage(create.error, 'No se pudo guardar el médico.')
          )}
        </div>
      )}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="min-h-11 flex-1 rounded-xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
        >
          Cancelar
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!name.trim() || create.isPending}
          className="min-h-11 flex-[2] rounded-xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
        >
          {create.isPending ? 'Guardando…' : 'Guardar médico'}
        </button>
      </div>
    </div>
  )
}

/**
 * Doctor field of the entry form: searchable list (name or alias, accent-
 * insensitive) with "Agregar doctor". `value` is a doctor id or null;
 * `fallback` ({id, name}) labels the current doctor before the list loads;
 * `label` names the field (default «Médico»).
 */
export default function DoctorPicker({ value, onChange, fallback, error, label = 'Médico' }) {
  const { data: doctors = [], isPending, isError } = useDoctors()
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [adding, setAdding] = useState(false)
  const inputId = useId()
  const listId = useId()
  // The button that was pressed often disappears (the list closes, the
  // chosen doctor replaces the search box): move focus to its replacement so
  // the sheet keeps the keyboard.
  const changeRef = useRef(null)
  const searchRef = useRef(null)
  const addRef = useRef(null)
  const focusNext = useRef(null)
  useEffect(() => {
    if (!focusNext.current) return
    const target = { change: changeRef, search: searchRef, add: addRef }[focusNext.current]
    target.current?.focus()
    focusNext.current = null
  })

  const selected = doctors.find((d) => d.id === value) ?? (value ? fallback : null)
  const results = useMemo(() => {
    const needle = normalizeText(query)
    const active = doctors.filter((d) => d.is_active)
    if (!needle) return active.slice(0, MAX_RESULTS)
    return active
      .filter((d) => [d.name, ...d.aliases].some((t) => normalizeText(t).includes(needle)))
      .slice(0, MAX_RESULTS)
  }, [doctors, query])

  function choose(doctor) {
    focusNext.current = 'change'
    onChange(doctor.id)
    setQuery('')
    setSearching(false)
    setAdding(false)
  }

  if (selected && !searching) {
    return (
      <div>
        <span className="text-sm font-medium text-slate-700">{label}</span>
        <div className="mt-1 flex min-h-12 items-center gap-2 rounded-2xl border border-slate-300 bg-white py-1 pr-1 pl-3">
          <UserIcon className="size-5 shrink-0 text-slate-400" aria-hidden="true" />
          <span className="min-w-0 flex-1">
            <span className="block truncate font-medium">{selected.name}</span>
            {selected.specialty && (
              <span className="block truncate text-sm text-slate-500">{selected.specialty}</span>
            )}
          </span>
          <button
            type="button"
            ref={changeRef}
            onClick={() => {
              focusNext.current = 'search'
              setSearching(true)
            }}
            className="min-h-11 rounded-xl px-3 text-sm font-medium text-brand-700 active:bg-brand-50"
          >
            Cambiar
          </button>
          <button
            type="button"
            onClick={() => {
              focusNext.current = 'search'
              onChange(null)
            }}
            aria-label="Quitar médico"
            className="flex size-11 items-center justify-center rounded-xl text-slate-500 active:bg-slate-100"
          >
            <XMarkIcon className="size-5" aria-hidden="true" />
          </button>
        </div>
      </div>
    )
  }

  return (
    <div>
      <label htmlFor={inputId} className="text-sm font-medium text-slate-700">
        {label} <span className="font-normal text-slate-500">(opcional)</span>
      </label>
      <input
        ref={searchRef}
        id={inputId}
        type="search"
        value={query}
        autoComplete="off"
        aria-controls={listId}
        aria-describedby={error ? `${inputId}-error` : undefined}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => setSearching(true)}
        placeholder="Buscar por nombre…"
        className={inputClass}
      />
      {error && (
        <p id={`${inputId}-error`} className="mt-1 text-sm text-red-700">
          {error}
        </p>
      )}
      {searching && !adding && (
        <div className="mt-1 rounded-2xl border border-slate-200 bg-white">
          <ul
            id={listId}
            aria-label="Médicos"
            className="max-h-72 divide-y divide-slate-100 overflow-y-auto"
          >
            {isPending && <li className="px-3 py-3 text-sm text-slate-500">Cargando médicos…</li>}
            {isError && (
              <li className="px-3 py-3 text-sm text-red-700">
                No se pudo cargar la lista de médicos.
              </li>
            )}
            {!isPending && !isError && results.length === 0 && (
              <li className="px-3 py-3 text-sm text-slate-500">
                {query ? 'Ningún médico coincide.' : 'Todavía no hay médicos.'}
              </li>
            )}
            {results.map((doctor) => (
              <li key={doctor.id}>
                <button
                  type="button"
                  onClick={() => choose(doctor)}
                  className="flex min-h-12 w-full flex-col justify-center px-3 py-1 text-left active:bg-slate-100"
                >
                  <span className="font-medium">{doctor.name}</span>
                  {doctor.specialty && (
                    <span className="text-sm text-slate-500">{doctor.specialty}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
          <div className="flex border-t border-slate-100">
            <button
              type="button"
              ref={addRef}
              onClick={() => setAdding(true)}
              className="flex min-h-12 flex-1 items-center gap-2 px-3 font-medium text-brand-700 active:bg-brand-50"
            >
              <PlusIcon className="size-5" aria-hidden="true" />
              Agregar doctor
            </button>
            <button
              type="button"
              onClick={() => {
                setSearching(false)
                setQuery('')
              }}
              className="min-h-12 px-4 text-sm font-medium text-slate-600 active:bg-slate-100"
            >
              {selected ? 'Mantener' : 'Cerrar'}
            </button>
          </div>
        </div>
      )}
      {adding && (
        <AddDoctorForm
          initialName={query.trim()}
          onCreated={choose}
          onCancel={() => {
            focusNext.current = 'add'
            setAdding(false)
          }}
        />
      )}
    </div>
  )
}
