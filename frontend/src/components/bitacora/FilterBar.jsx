import { useEffect, useMemo, useState } from 'react'
import {
  CheckIcon,
  ChevronDownIcon,
  MagnifyingGlassIcon,
  XMarkIcon,
} from '@heroicons/react/20/solid'
import Sheet from '../ui/Sheet.jsx'
import {
  DEFAULT_ORDER,
  ENTRY_TYPES,
  EMPTY_FILTERS,
  dateChipLabel,
  entryTypeLabel,
  hasActiveFilters,
  normalizeText,
  todayISO,
} from '../../lib/bitacora.js'

const SORT_OPTIONS = [
  { value: 'date:desc', label: 'Más recientes primero' },
  { value: 'date:asc', label: 'Más antiguas primero' },
  { value: 'doctor:asc', label: 'Médico (A–Z)' },
  { value: 'doctor:desc', label: 'Médico (Z–A)' },
  { value: 'type:asc', label: 'Tipo (A–Z)' },
  { value: 'type:desc', label: 'Tipo (Z–A)' },
]

export function Chip({ active, onClick, children, pressed }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={pressed}
      className={`flex min-h-11 shrink-0 items-center gap-1 rounded-full px-3.5 text-sm font-medium whitespace-nowrap ring-1 ring-inset ${
        active
          ? 'bg-brand-700 text-white ring-brand-700 active:bg-brand-800'
          : 'bg-white text-slate-700 ring-slate-300 active:bg-slate-100'
      }`}
    >
      {children}
    </button>
  )
}

/** The search box writes `q` to the URL 300 ms after the last keystroke. */
export function SearchBox({
  value,
  onSearch,
  id = 'bitacora-search',
  label = 'Buscar en la bitácora',
  placeholder = 'Buscar: neumonía, Dr. Pérez…',
}) {
  const [text, setText] = useState(value)
  const [synced, setSynced] = useState(value)
  // The URL changed from elsewhere ("Quitar filtros", back button): follow it.
  if (value !== synced) {
    setSynced(value)
    if (text.trim() !== value.trim()) setText(value)
  }

  useEffect(() => {
    if (text.trim() === value.trim()) return undefined
    const timer = setTimeout(() => onSearch(text), 300)
    return () => clearTimeout(timer)
  }, [text, value, onSearch])

  return (
    <form role="search" onSubmit={(e) => e.preventDefault()} className="relative">
      <label htmlFor={id} className="sr-only">
        {label}
      </label>
      <MagnifyingGlassIcon
        className="pointer-events-none absolute top-1/2 left-3 size-5 -translate-y-1/2 text-slate-400"
        aria-hidden="true"
      />
      <input
        id={id}
        type="search"
        enterKeyHint="search"
        maxLength={200}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        className="min-h-12 w-full rounded-2xl border border-slate-300 bg-white pr-11 pl-10 text-base placeholder:text-slate-400 focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none"
      />
      {text && (
        <button
          type="button"
          aria-label="Borrar búsqueda"
          onClick={() => {
            setText('')
            onSearch('')
          }}
          className="absolute top-1/2 right-1 flex size-10 -translate-y-1/2 items-center justify-center rounded-full text-slate-500 active:bg-slate-100"
        >
          <XMarkIcon className="size-5" aria-hidden="true" />
        </button>
      )}
    </form>
  )
}

function CheckList({ options, selected, onToggle, legend }) {
  return (
    <fieldset>
      <legend className="sr-only">{legend}</legend>
      <ul className="divide-y divide-slate-100">
        {options.map((option) => {
          const checked = selected.includes(option.value)
          return (
            <li key={option.value}>
              <label className="flex min-h-12 cursor-pointer items-center gap-3 py-1">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => onToggle(option.value)}
                  className="size-5 accent-brand-700"
                />
                <span className="flex-1">
                  {option.label}
                  {option.hint && (
                    <span className="block text-sm text-slate-500">{option.hint}</span>
                  )}
                </span>
              </label>
            </li>
          )
        })}
      </ul>
    </fieldset>
  )
}

function SheetFooter({ onClear, onApply }) {
  return (
    <div className="flex gap-2">
      <button
        type="button"
        onClick={onClear}
        className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
      >
        Quitar
      </button>
      <button
        type="button"
        onClick={onApply}
        className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800"
      >
        Aplicar
      </button>
    </div>
  )
}

function toggle(list, value) {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value]
}

function DoctorFilterSheet({ doctors, selected, onApply, onClose }) {
  const [draft, setDraft] = useState(selected)
  const [query, setQuery] = useState('')
  const options = useMemo(() => {
    const needle = normalizeText(query)
    return doctors
      .filter(
        (d) =>
          !needle || [d.name, ...d.aliases].some((text) => normalizeText(text).includes(needle)),
      )
      .map((d) => ({ value: d.id, label: d.name, hint: d.specialty }))
  }, [doctors, query])

  return (
    <Sheet
      title="Filtrar por médico"
      onClose={onClose}
      footer={<SheetFooter onClear={() => onApply([])} onApply={() => onApply(draft)} />}
    >
      <label htmlFor="doctor-filter-search" className="text-sm font-medium text-slate-700">
        Buscar médico
      </label>
      <input
        id="doctor-filter-search"
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        className="mt-1 mb-2 min-h-12 w-full rounded-2xl border border-slate-300 px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none"
      />
      {options.length ? (
        <CheckList
          legend="Médicos"
          options={options}
          selected={draft}
          onToggle={(id) => setDraft((d) => toggle(d, id))}
        />
      ) : (
        <p className="py-6 text-center text-slate-500">
          {doctors.length ? 'Ningún médico coincide.' : 'Todavía no hay médicos.'}
        </p>
      )}
    </Sheet>
  )
}

function TypeFilterSheet({ selected, onApply, onClose }) {
  const [draft, setDraft] = useState(selected)
  return (
    <Sheet
      title="Filtrar por tipo"
      onClose={onClose}
      footer={<SheetFooter onClear={() => onApply([])} onApply={() => onApply(draft)} />}
    >
      <CheckList
        legend="Tipos de entrada"
        options={ENTRY_TYPES}
        selected={draft}
        onToggle={(t) => setDraft((d) => toggle(d, t))}
      />
    </Sheet>
  )
}

function shiftDays(iso, days) {
  const [y, m, d] = iso.split('-').map(Number)
  const date = new Date(Date.UTC(y, m - 1, d + days))
  return date.toISOString().slice(0, 10)
}

export function DateFilterSheet({ dateFrom, dateTo, onApply, onClose }) {
  const [from, setFrom] = useState(dateFrom)
  const [to, setTo] = useState(dateTo)
  const today = todayISO()
  const presets = [
    { label: 'Últimos 30 días', from: shiftDays(today, -30), to: '' },
    { label: 'Últimos 6 meses', from: shiftDays(today, -182), to: '' },
    { label: 'Este año', from: `${today.slice(0, 4)}-01-01`, to: '' },
  ]
  const invalid = from && to && from > to
  const inputClass =
    'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none'

  return (
    <Sheet
      title="Filtrar por fechas"
      onClose={onClose}
      footer={
        <SheetFooter
          onClear={() => onApply('', '')}
          onApply={() => !invalid && onApply(from, to)}
        />
      }
    >
      <div className="flex flex-wrap gap-2">
        {presets.map((p) => (
          <Chip
            key={p.label}
            active={from === p.from && to === p.to}
            onClick={() => {
              setFrom(p.from)
              setTo(p.to)
            }}
          >
            {p.label}
          </Chip>
        ))}
      </div>
      <div className="mt-4 grid grid-cols-2 gap-3">
        <div>
          <label htmlFor="filter-date-from" className="text-sm font-medium text-slate-700">
            Desde
          </label>
          <input
            id="filter-date-from"
            type="date"
            value={from}
            max={to || undefined}
            onChange={(e) => setFrom(e.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor="filter-date-to" className="text-sm font-medium text-slate-700">
            Hasta
          </label>
          <input
            id="filter-date-to"
            type="date"
            value={to}
            min={from || undefined}
            onChange={(e) => setTo(e.target.value)}
            className={inputClass}
          />
        </div>
      </div>
      {invalid && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          La fecha «desde» debe ser anterior a «hasta».
        </p>
      )}
    </Sheet>
  )
}

/**
 * Search, filter chips (médico, tipo, fechas, por revisar) and the sort
 * control. Every change goes through `onChange(filters)` (which writes the URL).
 */
export default function FilterBar({ filters, onChange, doctors, showSort }) {
  const [open, setOpen] = useState(null) // 'doctor' | 'type' | 'date'
  const close = () => setOpen(null)
  const update = (patch) => {
    onChange({ ...filters, ...patch, page: 1 })
    close()
  }

  const doctorNames = Object.fromEntries(doctors.map((d) => [d.id, d.name]))
  const doctorLabel =
    filters.doctorIds.length === 0
      ? 'Médico'
      : filters.doctorIds.length === 1
        ? (doctorNames[filters.doctorIds[0]] ?? '1 médico')
        : `${filters.doctorIds.length} médicos`
  const typeLabel =
    filters.types.length === 0
      ? 'Tipo'
      : filters.types.length === 1
        ? entryTypeLabel(filters.types[0])
        : `${filters.types.length} tipos`
  const hasDates = Boolean(filters.dateFrom || filters.dateTo)

  return (
    <div className="space-y-3">
      <SearchBox value={filters.q} onSearch={(q) => onChange({ ...filters, q, page: 1 })} />

      <div
        className="chip-scroll -mx-4 flex gap-2 overflow-x-auto px-4 pb-1 lg:mx-0 lg:flex-wrap lg:px-0"
        role="group"
        aria-label="Filtros"
      >
        <Chip active={filters.doctorIds.length > 0} onClick={() => setOpen('doctor')}>
          {filters.doctorIds.length > 0 && <CheckIcon className="size-4" aria-hidden="true" />}
          <span className="max-w-48 truncate">{doctorLabel}</span>
          <ChevronDownIcon className="size-4" aria-hidden="true" />
        </Chip>
        <Chip active={filters.types.length > 0} onClick={() => setOpen('type')}>
          {filters.types.length > 0 && <CheckIcon className="size-4" aria-hidden="true" />}
          {typeLabel}
          <ChevronDownIcon className="size-4" aria-hidden="true" />
        </Chip>
        <Chip active={hasDates} onClick={() => setOpen('date')}>
          {hasDates && <CheckIcon className="size-4" aria-hidden="true" />}
          {dateChipLabel(filters.dateFrom, filters.dateTo)}
          <ChevronDownIcon className="size-4" aria-hidden="true" />
        </Chip>
        <Chip
          active={filters.pendingOnly}
          pressed={filters.pendingOnly}
          onClick={() => onChange({ ...filters, pendingOnly: !filters.pendingOnly, page: 1 })}
        >
          {filters.pendingOnly && <CheckIcon className="size-4" aria-hidden="true" />}
          Por revisar
        </Chip>
        {hasActiveFilters(filters) && (
          <button
            type="button"
            onClick={() => onChange({ ...EMPTY_FILTERS, sort: filters.sort, order: filters.order })}
            className="min-h-11 shrink-0 rounded-full px-3 text-sm font-medium whitespace-nowrap text-brand-700 underline active:bg-brand-50"
          >
            Quitar filtros
          </button>
        )}
      </div>

      {showSort && (
        <div className="flex items-center gap-2">
          <label htmlFor="bitacora-sort" className="text-sm text-slate-600">
            Ordenar:
          </label>
          <select
            id="bitacora-sort"
            value={`${filters.sort}:${filters.order}`}
            onChange={(e) => {
              const [sort, order] = e.target.value.split(':')
              onChange({ ...filters, sort, order: order ?? DEFAULT_ORDER[sort], page: 1 })
            }}
            className="min-h-11 flex-1 rounded-xl border border-slate-300 bg-white px-2 text-sm focus:border-brand-600 focus:outline-none"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      )}

      {open === 'doctor' && (
        <DoctorFilterSheet
          doctors={doctors}
          selected={filters.doctorIds}
          onApply={(doctorIds) => update({ doctorIds })}
          onClose={close}
        />
      )}
      {open === 'type' && (
        <TypeFilterSheet
          selected={filters.types}
          onApply={(types) => update({ types })}
          onClose={close}
        />
      )}
      {open === 'date' && (
        <DateFilterSheet
          dateFrom={filters.dateFrom}
          dateTo={filters.dateTo}
          onApply={(dateFrom, dateTo) => update({ dateFrom, dateTo })}
          onClose={close}
        />
      )}
    </div>
  )
}
