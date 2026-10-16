import { lazy, Suspense, useCallback, useMemo, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import {
  ArrowUpTrayIcon,
  BeakerIcon,
  ExclamationTriangleIcon,
  MagnifyingGlassIcon,
} from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { usePatientsQuery } from '../lib/patients.js'
import { useLabReportsInfinite, usePendingLabCount } from '../lib/labReports.js'
import { errorMessage, formatLongDate } from '../lib/bitacora.js'
import {
  EMPTY_LAB_FILTERS,
  groupByDate,
  hasActiveLabFilters,
  labFiltersToSearchParams,
  parseLabFilters,
} from '../lib/labs.js'
import LabFilterBar from '../components/labs/LabFilterBar.jsx'
import ReportCard from '../components/labs/ReportCard.jsx'
import PendingLinks from '../components/labs/PendingLinks.jsx'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'

const PAGE_SIZE = 30

// Split out: only loaded when someone uploads.
const UploadSheet = lazy(() => import('../components/labs/UploadSheet.jsx'))

/** «N informes por revisar» with a shortcut to the «Por revisar» filter. */
function PendingBanner({ count, onShow }) {
  if (!count) return null
  return (
    <button
      type="button"
      onClick={onShow}
      className="mb-4 flex min-h-12 w-full items-center gap-3 rounded-2xl border border-amber-300 bg-amber-50 px-4 py-2 text-left active:bg-amber-100"
    >
      <ExclamationTriangleIcon className="size-6 shrink-0 text-amber-700" aria-hidden="true" />
      <span className="flex-1 font-medium text-amber-950">
        {count === 1 ? '1 informe por revisar' : `${count} informes por revisar`}
      </span>
      <span className="text-sm font-semibold text-amber-900 underline">Ver</span>
    </button>
  )
}

function countLabel(total) {
  return total === 1 ? '1 informe' : `${total} informes`
}

function Empty({ filtered, onClear }) {
  const Icon = filtered ? MagnifyingGlassIcon : BeakerIcon
  return (
    <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
      <Icon className="mx-auto size-10 text-slate-400" aria-hidden="true" />
      <p className="mt-3 font-medium text-slate-800">
        {filtered ? 'Ningún informe coincide con los filtros.' : 'Todavía no hay informes.'}
      </p>
      <p className="mt-1 text-sm text-slate-500">
        {filtered
          ? 'Prueba con otro examen o quita algún filtro.'
          : 'Los PDF de laboratorio que lleguen por WhatsApp o que subas con «Subir resultado» aparecerán aquí.'}
      </p>
      {filtered && (
        <button
          type="button"
          onClick={onClear}
          className="mt-5 min-h-12 rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
        >
          Quitar filtros
        </button>
      )}
    </div>
  )
}

/**
 * Labs tab: reports for the selected patient(s), grouped by report date.
 * Reports whose patient was not identified show under every selection, with
 * a «Paciente sin identificar» badge, until someone assigns them.
 */
export default function LabsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => parseLabFilters(searchParams), [searchParams])
  const location = useLocation()
  const { patient, patients, patientIds, isLoading: patientsLoading } = usePatient()
  const patientsQuery = usePatientsQuery()

  const ready = !patientsLoading && !patientsQuery.isError
  const query = useLabReportsInfinite(filters, patientIds, { enabled: ready, pageSize: PAGE_SIZE })
  const reports = query.data?.pages.flatMap((page) => page.items)
  const total = query.data?.pages[0]?.total
  const groups = useMemo(() => groupByDate(reports ?? []), [reports])
  const pending = usePendingLabCount(patientIds, { enabled: ready && !filters.pendingOnly })
  // false, or { linkId } («Subir PDF» from a pending link; null = «Subir resultado»).
  const [uploading, setUploading] = useState(false)

  // Typing replaces the history entry; chips push one.
  const setFilters = useCallback(
    (next) => {
      const replace = Object.keys(next).every((k) => k === 'q' || next[k] === filters[k])
      setSearchParams(labFiltersToSearchParams(next), { replace })
    },
    [filters, setSearchParams],
  )

  const linkState = useMemo(() => ({ from: location.search }), [location.search])
  const patientNames = useMemo(
    () =>
      patient === 'ambos' ? Object.fromEntries(patients.map((p) => [p.id, p.display_name])) : {},
    [patient, patients],
  )
  const filtered = hasActiveLabFilters(filters)

  return (
    <section>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div>
          <h1 className="text-2xl font-semibold">Laboratorios</h1>
          <p className="mt-0.5 text-sm text-slate-500" aria-live="polite">
            {total !== undefined ? countLabel(total) : ' '}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setUploading({ linkId: null })}
          className="flex min-h-12 shrink-0 items-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800"
        >
          <ArrowUpTrayIcon className="size-5" aria-hidden="true" />
          Subir resultado
        </button>
      </div>

      {!filters.pendingOnly && (
        <PendingBanner
          count={pending.data?.total}
          onShow={() => setFilters({ ...EMPTY_LAB_FILTERS, pendingOnly: true })}
        />
      )}

      {ready && (
        <PendingLinks
          patientIds={patientIds}
          patients={patients}
          onUpload={(link) => setUploading({ linkId: link.id })}
        />
      )}

      <LabFilterBar filters={filters} onChange={setFilters} />

      <div className="mt-4">
        {patientsQuery.isError ? (
          <ErrorState
            message={errorMessage(patientsQuery.error, 'No se pudo cargar la lista de pacientes.')}
            onRetry={() => patientsQuery.refetch()}
          />
        ) : !ready || query.isPending ? (
          <CardSkeletons />
        ) : query.isError ? (
          <ErrorState
            message={errorMessage(query.error, 'No se pudieron cargar los informes.')}
            onRetry={() => query.refetch()}
          />
        ) : !reports.length ? (
          <Empty filtered={filtered} onClear={() => setFilters({ ...EMPTY_LAB_FILTERS })} />
        ) : (
          <>
            <div className="space-y-5">
              {groups.map((group) => (
                <section key={group.date} aria-labelledby={`labs-day-${group.date}`}>
                  <h2
                    id={`labs-day-${group.date}`}
                    className="mb-2 text-sm font-semibold text-slate-600 first-letter:uppercase"
                  >
                    <time dateTime={group.date}>{formatLongDate(group.date)}</time>
                  </h2>
                  <ul className="space-y-3">
                    {group.reports.map((report) => (
                      <li key={report.id}>
                        <ReportCard
                          report={report}
                          patientName={patientNames[report.patient_id]}
                          linkState={linkState}
                        />
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
            {query.hasNextPage && (
              <button
                type="button"
                onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage}
                className="mt-4 min-h-12 w-full rounded-2xl bg-white font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
              >
                {query.isFetchingNextPage
                  ? 'Cargando…'
                  : `Cargar más (${reports.length} de ${total})`}
              </button>
            )}
            {query.isFetchNextPageError && (
              <p role="alert" className="mt-2 text-center text-sm text-red-700">
                No se pudieron cargar más informes.
              </p>
            )}
          </>
        )}
      </div>

      {uploading && (
        <Suspense fallback={null}>
          <UploadSheet labLinkId={uploading.linkId} onClose={() => setUploading(false)} />
        </Suspense>
      )}
    </section>
  )
}
