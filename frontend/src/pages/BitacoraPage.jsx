import { lazy, Suspense, useCallback, useMemo, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { CheckCircleIcon, CheckIcon, PlusIcon, XMarkIcon } from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { useMe } from '../lib/auth.js'
import { useDoctors } from '../lib/doctors.js'
import { usePatientsQuery } from '../lib/patients.js'
import { useBulkReview, useLogEntriesInfinite, useLogEntriesPage } from '../lib/logEntries.js'
import { useIsDesktop } from '../lib/useMediaQuery.js'
import {
  EMPTY_FILTERS,
  errorMessage,
  filtersToSearchParams,
  hasActiveFilters,
  parseFilters,
  toReviewFilter,
} from '../lib/bitacora.js'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'
import FilterBar from '../components/bitacora/FilterBar.jsx'
import EntryCard from '../components/bitacora/EntryCard.jsx'
import EntrySheet from '../components/bitacora/EntrySheet.jsx'
import NextAppointmentCard from '../components/calendar/NextAppointmentCard.jsx'
import {
  CardSkeletons,
  EmptyState,
  ErrorState,
  TableSkeleton,
} from '../components/bitacora/ListStates.jsx'

// Desktop only, so phones never download the table library.
const EntryTable = lazy(() => import('../components/bitacora/EntryTable.jsx'))

const PAGE_SIZE_MOBILE = 20
const PAGE_SIZE_DESKTOP = 25
const NO_DOCTORS = []

function countLabel(total) {
  return total === 1 ? '1 entrada' : `${total} entradas`
}

function Pager({ page, total, pageSize, onPage }) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const button =
    'min-h-11 rounded-xl px-4 font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-100 disabled:opacity-40'
  return (
    <nav aria-label="Páginas" className="mt-4 flex items-center justify-between gap-4">
      <button
        type="button"
        className={button}
        disabled={page <= 1}
        onClick={() => onPage(page - 1)}
      >
        Anterior
      </button>
      <span className="text-sm text-slate-600">
        Página {Math.min(page, pages)} de {pages}
      </span>
      <button
        type="button"
        className={button}
        disabled={page >= pages}
        onClick={() => onPage(page + 1)}
      >
        Siguiente
      </button>
    </nav>
  )
}

export default function BitacoraPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => parseFilters(searchParams), [searchParams])
  const location = useLocation()
  const isDesktop = useIsDesktop()
  const { patient, patients, patientIds, isLoading: patientsLoading } = usePatient()
  const patientsQuery = usePatientsQuery()
  const { data: doctors = NO_DOCTORS } = useDoctors()
  const [creating, setCreating] = useState(false)
  const [savedId, setSavedId] = useState(null)
  const { data: me } = useMe()
  const bulkReview = useBulkReview()
  const [confirmingBulk, setConfirmingBulk] = useState(false)
  const [reviewedCount, setReviewedCount] = useState(null)

  // Without the patient list the Papá/Mamá filter can't be applied: never
  // fall back to an unfiltered list.
  const ready = !patientsLoading && !patientsQuery.isError
  const infinite = useLogEntriesInfinite(filters, patientIds, {
    enabled: ready && !isDesktop,
    pageSize: PAGE_SIZE_MOBILE,
  })
  const paged = useLogEntriesPage(filters, patientIds, {
    enabled: ready && isDesktop,
    pageSize: PAGE_SIZE_DESKTOP,
  })
  const query = isDesktop ? paged : infinite
  const entries = isDesktop ? paged.data?.items : infinite.data?.pages.flatMap((page) => page.items)
  const total = isDesktop ? paged.data?.total : infinite.data?.pages[0]?.total

  // Typing in the search box replaces the history entry; chips, sort and
  // pages push one, so the back button undoes them.
  const setFilters = useCallback(
    (next) => {
      const replace = Object.keys(next).every(
        (k) => k === 'q' || k === 'page' || next[k] === filters[k],
      )
      setSearchParams(filtersToSearchParams(next), { replace })
    },
    [filters, setSearchParams],
  )
  const clearFilters = () =>
    setFilters({ ...EMPTY_FILTERS, sort: filters.sort, order: filters.order })

  const linkState = useMemo(() => ({ from: location.search }), [location.search])
  const patientNames = useMemo(
    () =>
      patient === 'ambos' ? Object.fromEntries(patients.map((p) => [p.id, p.display_name])) : {},
    [patient, patients],
  )
  const filtered = hasActiveFilters(filters)
  const loading = !ready || query.isPending
  // «Marcar todas como revisadas»: admins, with «Por revisar» on and something to review.
  const canBulkReview = Boolean(me?.is_admin) && filters.pendingOnly && ready && total > 0

  const openBulkReview = () => {
    bulkReview.reset()
    setConfirmingBulk(true)
  }
  const confirmBulkReview = () =>
    bulkReview.mutate(toReviewFilter(filters, patientIds), {
      onSuccess: (result) => {
        setConfirmingBulk(false)
        setReviewedCount(result.updated)
      },
    })

  return (
    // Bottom padding keeps the last card clear of the floating button.
    <section className="pb-16 lg:pb-0">
      <div className="mb-4 flex items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Bitácora</h1>
          <p className="mt-0.5 text-sm text-slate-500" aria-live="polite">
            {total !== undefined ? countLabel(total) : ' '}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="hidden min-h-11 items-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white hover:bg-brand-800 lg:flex"
        >
          <PlusIcon className="size-5" aria-hidden="true" />
          Nueva entrada
        </button>
      </div>

      {savedId && (
        <div
          role="status"
          className="mb-4 flex items-center gap-2 rounded-2xl border border-brand-100 bg-brand-50 py-1 pr-1 pl-3 text-brand-800"
        >
          <CheckCircleIcon className="size-5 shrink-0" aria-hidden="true" />
          <span className="flex-1 font-medium">Entrada guardada.</span>
          <Link
            to={`/bitacora/${savedId}`}
            state={linkState}
            className="flex min-h-11 items-center rounded-xl px-3 font-medium underline"
          >
            Ver
          </Link>
          <button
            type="button"
            onClick={() => setSavedId(null)}
            aria-label="Cerrar aviso"
            className="flex size-11 items-center justify-center rounded-xl active:bg-brand-100"
          >
            <XMarkIcon className="size-5" aria-hidden="true" />
          </button>
        </div>
      )}

      <NextAppointmentCard enabled={ready} />

      <FilterBar filters={filters} onChange={setFilters} doctors={doctors} showSort={!isDesktop} />

      {reviewedCount !== null && (
        <div
          role="status"
          className="mt-4 flex items-center gap-2 rounded-2xl border border-brand-100 bg-brand-50 py-1 pr-1 pl-3 text-brand-800"
        >
          <CheckCircleIcon className="size-5 shrink-0" aria-hidden="true" />
          <span className="flex-1 font-medium">
            {reviewedCount === 1
              ? '1 entrada marcada como revisada.'
              : `${reviewedCount} entradas marcadas como revisadas.`}
          </span>
          <button
            type="button"
            onClick={() => setReviewedCount(null)}
            aria-label="Cerrar aviso"
            className="flex size-11 items-center justify-center rounded-xl active:bg-brand-100"
          >
            <XMarkIcon className="size-5" aria-hidden="true" />
          </button>
        </div>
      )}

      {canBulkReview && (
        <button
          type="button"
          onClick={openBulkReview}
          className="mt-4 flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl bg-white px-4 font-medium text-brand-700 ring-1 ring-brand-600 active:bg-brand-50 sm:w-auto"
        >
          <CheckIcon className="size-5" aria-hidden="true" />
          Marcar todas como revisadas
        </button>
      )}

      <div className="mt-4">
        {patientsQuery.isError ? (
          <ErrorState
            message={errorMessage(patientsQuery.error, 'No se pudo cargar la lista de pacientes.')}
            onRetry={() => patientsQuery.refetch()}
          />
        ) : loading ? (
          isDesktop ? (
            <TableSkeleton />
          ) : (
            <CardSkeletons />
          )
        ) : query.isError ? (
          <ErrorState
            message={errorMessage(query.error, 'No se pudo cargar la bitácora.')}
            onRetry={() => query.refetch()}
          />
        ) : !entries?.length ? (
          isDesktop && filters.page > 1 ? (
            <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-8 text-center">
              <p className="text-slate-700">No hay entradas en esta página.</p>
              <button
                type="button"
                onClick={() => setFilters({ ...filters, page: 1 })}
                className="mt-4 min-h-11 rounded-2xl px-4 font-medium text-brand-700 ring-1 ring-brand-600"
              >
                Ir a la primera página
              </button>
            </div>
          ) : (
            <EmptyState
              filtered={filtered}
              onClear={clearFilters}
              onCreate={() => setCreating(true)}
            />
          )
        ) : isDesktop ? (
          <div className={paged.isPlaceholderData ? 'opacity-60 transition-opacity' : ''}>
            <Suspense fallback={<TableSkeleton />}>
              <EntryTable
                entries={entries}
                filters={filters}
                onSort={(sort, order) => setFilters({ ...filters, sort, order, page: 1 })}
                showPatient={patient === 'ambos'}
                patientNames={patientNames}
                linkState={linkState}
              />
            </Suspense>
            <Pager
              page={filters.page}
              total={total}
              pageSize={PAGE_SIZE_DESKTOP}
              onPage={(page) => setFilters({ ...filters, page })}
            />
          </div>
        ) : (
          <>
            <ul className="space-y-3">
              {entries.map((entry) => (
                <li key={entry.id}>
                  <EntryCard
                    entry={entry}
                    patientName={patientNames[entry.patient_id]}
                    linkState={linkState}
                  />
                </li>
              ))}
            </ul>
            {infinite.hasNextPage && (
              <button
                type="button"
                onClick={() => infinite.fetchNextPage()}
                disabled={infinite.isFetchingNextPage}
                className="mt-4 min-h-12 w-full rounded-2xl bg-white font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
              >
                {infinite.isFetchingNextPage
                  ? 'Cargando…'
                  : `Cargar más (${entries.length} de ${total})`}
              </button>
            )}
            {infinite.isFetchNextPageError && (
              <p role="alert" className="mt-2 text-center text-sm text-red-700">
                No se pudieron cargar más entradas.
              </p>
            )}
          </>
        )}
      </div>

      {/* Quick add, above the bottom nav (phones). */}
      <button
        type="button"
        onClick={() => setCreating(true)}
        aria-label="Nueva entrada"
        className="fixed right-4 bottom-[calc(5rem+env(safe-area-inset-bottom))] z-10 flex size-14 items-center justify-center rounded-full bg-brand-700 text-white shadow-lg active:bg-brand-800 lg:hidden"
      >
        <PlusIcon className="size-7" aria-hidden="true" />
      </button>

      {confirmingBulk && (
        <ConfirmDialog
          title="Marcar todas como revisadas"
          message={
            total === 1
              ? 'Se marcará como revisada 1 entrada pendiente, la que muestra la lista con los filtros actuales.'
              : `Se marcarán como revisadas ${total} entradas pendientes, las que muestra la lista con los filtros actuales.`
          }
          confirmLabel={total === 1 ? 'Marcar 1 entrada' : `Marcar ${total} entradas`}
          busy={bulkReview.isPending}
          error={
            bulkReview.isError
              ? errorMessage(bulkReview.error, 'No se pudieron marcar las entradas.')
              : ''
          }
          onConfirm={confirmBulkReview}
          onCancel={() => setConfirmingBulk(false)}
        />
      )}

      {creating && (
        <EntrySheet
          onClose={() => setCreating(false)}
          onSaved={(entry) => {
            setCreating(false)
            setSavedId(entry.id)
          }}
        />
      )}
    </section>
  )
}
