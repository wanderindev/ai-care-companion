import { useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  createColumnHelper,
  rowSortingFeature,
  tableFeatures,
  useTable,
} from '@tanstack/react-table'
import {
  ArrowDownIcon,
  ArrowUpIcon,
  ChevronUpDownIcon,
  PaperClipIcon,
} from '@heroicons/react/20/solid'
import { DEFAULT_ORDER, entrySource, formatDate, formatTime } from '../../lib/bitacora.js'
import { PatientTag, ReviewBadge, TypeChip } from './EntryBadges.jsx'

// Sorting is done by the API (sort/order in the URL); the table only shows it.
const features = tableFeatures({ rowSortingFeature })
const helper = createColumnHelper()
const SORT_LABELS = { date: 'fecha', doctor: 'médico', type: 'tipo' }
const NO_ROWS = []

function buildColumns(showPatient, patientNames, linkState) {
  return helper.columns([
    helper.accessor('entry_date', {
      id: 'date',
      header: 'Fecha',
      cell: ({ row }) => (
        <span className="whitespace-nowrap">
          {formatDate(row.original.entry_date)}
          {row.original.entry_time && (
            <span className="block text-xs text-slate-500">
              {formatTime(row.original.entry_time)}
            </span>
          )}
        </span>
      ),
    }),
    ...(showPatient
      ? [
          helper.accessor('patient_id', {
            id: 'patient',
            header: 'Paciente',
            enableSorting: false,
            cell: ({ getValue }) => <PatientTag name={patientNames[getValue()]} />,
          }),
        ]
      : []),
    helper.accessor((entry) => entrySource(entry), {
      id: 'doctor',
      header: 'Médico / fuente',
      cell: ({ getValue }) => <span className="font-medium">{getValue()}</span>,
    }),
    helper.accessor('entry_type', {
      id: 'type',
      header: 'Tipo',
      cell: ({ getValue }) => <TypeChip type={getValue()} />,
    }),
    helper.display({
      id: 'title',
      header: 'Motivo',
      cell: ({ row }) => (
        <Link
          to={`/bitacora/${row.original.id}`}
          state={linkState}
          onClick={(e) => e.stopPropagation()}
          className="font-medium text-slate-900 hover:underline"
        >
          {row.original.title || <span className="text-slate-500">(sin título)</span>}
        </Link>
      ),
    }),
    helper.display({
      id: 'files',
      header: () => (
        <>
          <PaperClipIcon className="size-4" aria-hidden="true" />
          <span className="sr-only">Adjuntos</span>
        </>
      ),
      cell: ({ row }) => row.original.attachments.length || '',
    }),
    helper.display({
      id: 'review',
      header: () => <span className="sr-only">Estado</span>,
      cell: ({ row }) => (row.original.review_status === 'pending_review' ? <ReviewBadge /> : null),
    }),
  ])
}

/**
 * Desktop table (≥1024 px). Clicking a sortable header toggles the API sort;
 * clicking a row opens the entry.
 */
export default function EntryTable({
  entries,
  filters,
  onSort,
  showPatient,
  patientNames,
  linkState,
}) {
  const navigate = useNavigate()
  const columns = useMemo(
    () => buildColumns(showPatient, patientNames, linkState),
    [showPatient, patientNames, linkState],
  )
  const sorting = useMemo(
    () => [{ id: filters.sort, desc: filters.order === 'desc' }],
    [filters.sort, filters.order],
  )

  const table = useTable({
    features,
    columns,
    data: entries ?? NO_ROWS,
    getRowId: (row) => String(row.id),
    state: { sorting },
    manualSorting: true,
    enableSortingRemoval: false,
    enableMultiSort: false,
    onSortingChange: (updater) => {
      const next = typeof updater === 'function' ? updater(sorting) : updater
      const first = next[0]
      if (!first) return
      // A new column starts in its natural direction (dates newest first).
      const order =
        first.id === filters.sort ? (first.desc ? 'desc' : 'asc') : DEFAULT_ORDER[first.id]
      onSort(first.id, order)
    },
  })

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => {
                const column = header.column
                const canSort = column.getCanSort()
                const sorted = column.getIsSorted()
                return (
                  <th
                    key={header.id}
                    scope="col"
                    aria-sort={
                      sorted ? (sorted === 'desc' ? 'descending' : 'ascending') : undefined
                    }
                    className="px-4 py-2 font-medium"
                  >
                    {canSort ? (
                      <button
                        type="button"
                        onClick={column.getToggleSortingHandler()}
                        title={`Ordenar por ${SORT_LABELS[column.id]}`}
                        className="-mx-2 flex min-h-10 items-center gap-1 rounded-lg px-2 hover:bg-slate-200/60"
                      >
                        <table.FlexRender header={header} />
                        {sorted === 'asc' && <ArrowUpIcon className="size-4" aria-hidden="true" />}
                        {sorted === 'desc' && (
                          <ArrowDownIcon className="size-4" aria-hidden="true" />
                        )}
                        {!sorted && (
                          <ChevronUpDownIcon className="size-4 text-slate-400" aria-hidden="true" />
                        )}
                      </button>
                    ) : (
                      <span className="flex items-center">
                        <table.FlexRender header={header} />
                      </span>
                    )}
                  </th>
                )
              })}
            </tr>
          ))}
        </thead>
        <tbody className="divide-y divide-slate-100">
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              onClick={() => navigate(`/bitacora/${row.original.id}`, { state: linkState })}
              className="cursor-pointer align-top hover:bg-slate-50"
            >
              {row.getAllCells().map((cell) => (
                <td key={cell.id} className="px-4 py-3">
                  <table.FlexRender cell={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
