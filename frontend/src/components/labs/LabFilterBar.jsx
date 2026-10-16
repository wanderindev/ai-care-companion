import { useState } from 'react'
import { CheckIcon, ChevronDownIcon } from '@heroicons/react/20/solid'
import { Chip, DateFilterSheet, SearchBox } from '../bitacora/FilterBar.jsx'
import { dateChipLabel } from '../../lib/bitacora.js'
import { EMPTY_LAB_FILTERS, hasActiveLabFilters } from '../../lib/labs.js'

/**
 * Test-name search and the chips (fechas, fuera de rango, por revisar).
 * Every change goes through `onChange(filters)`, which writes the URL.
 */
export default function LabFilterBar({ filters, onChange }) {
  const [dateOpen, setDateOpen] = useState(false)
  const hasDates = Boolean(filters.dateFrom || filters.dateTo)

  return (
    <div className="space-y-3">
      <SearchBox
        id="labs-search"
        label="Buscar por examen"
        placeholder="Examen: glucosa, B12, hemoglobina…"
        value={filters.q}
        onSearch={(q) => onChange({ ...filters, q })}
      />
      <div
        className="chip-scroll -mx-4 flex gap-2 overflow-x-auto px-4 pb-1 lg:mx-0 lg:flex-wrap lg:px-0"
        role="group"
        aria-label="Filtros"
      >
        <Chip
          active={filters.outOfRange}
          pressed={filters.outOfRange}
          onClick={() => onChange({ ...filters, outOfRange: !filters.outOfRange })}
        >
          {filters.outOfRange && <CheckIcon className="size-4" aria-hidden="true" />}
          Fuera de rango
        </Chip>
        <Chip active={hasDates} onClick={() => setDateOpen(true)}>
          {hasDates && <CheckIcon className="size-4" aria-hidden="true" />}
          {dateChipLabel(filters.dateFrom, filters.dateTo)}
          <ChevronDownIcon className="size-4" aria-hidden="true" />
        </Chip>
        <Chip
          active={filters.pendingOnly}
          pressed={filters.pendingOnly}
          onClick={() => onChange({ ...filters, pendingOnly: !filters.pendingOnly })}
        >
          {filters.pendingOnly && <CheckIcon className="size-4" aria-hidden="true" />}
          Por revisar
        </Chip>
        {hasActiveLabFilters(filters) && (
          <button
            type="button"
            onClick={() => onChange({ ...EMPTY_LAB_FILTERS })}
            className="min-h-11 shrink-0 rounded-full px-3 text-sm font-medium whitespace-nowrap text-brand-700 underline active:bg-brand-50"
          >
            Quitar filtros
          </button>
        )}
      </div>
      {dateOpen && (
        <DateFilterSheet
          dateFrom={filters.dateFrom}
          dateTo={filters.dateTo}
          onApply={(dateFrom, dateTo) => {
            onChange({ ...filters, dateFrom, dateTo })
            setDateOpen(false)
          }}
          onClose={() => setDateOpen(false)}
        />
      )}
    </div>
  )
}
