import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowLeftIcon,
  BookOpenIcon,
  CheckIcon,
  ClockIcon,
  PencilSquareIcon,
  TrashIcon,
} from '@heroicons/react/24/outline'
import { ChevronRightIcon } from '@heroicons/react/20/solid'
import { usePatient } from '../contexts/PatientContext.js'
import { panamaToday } from '../lib/appointments.js'
import { errorMessage, formatDate } from '../lib/bitacora.js'
import { formatMg } from '../lib/ledd.js'
import {
  buildTimeline,
  effectiveDate,
  EVENT_KIND_LABELS,
  monthLabel,
  useConfirmMedEvents,
  useDeleteMedEvent,
  useMedEvents,
  useMedVersions,
  usePendingMedEvents,
  useUpdateMedEvent,
} from '../lib/meds.js'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'
import MedEventSheet from '../components/meds/MedEventSheet.jsx'

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`
}

/** A saved version of the list: opens its detail (diff vs the previous one). */
function VersionItem({ version, patientId }) {
  const since = effectiveDate(version)
  const savedOn = formatDate(panamaToday(new Date(version.changed_at)))
  const counts = [plural(version.item_count, 'medicamento', 'medicamentos')]
  if (version.avoid_count) counts.push(`${version.avoid_count} en EVITAR`)
  if (version.ledd_mg != null) counts.push(`LEDD ≈ ${formatMg(version.ledd_mg)} mg/día`)
  return (
    <Link
      to={`/medicamentos/historial/${version.version_no}?paciente=${patientId}`}
      className="flex items-start gap-2 rounded-2xl bg-white p-4 ring-1 ring-slate-200 active:bg-slate-50"
    >
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-slate-900">
          Versión {version.version_no}
          {version.version_no === 1 && <span className="font-normal"> · Lista inicial</span>}
        </p>
        <p className="text-sm text-slate-600">
          {[since && `Desde ${formatDate(since)}`, version.changed_by_name]
            .filter(Boolean)
            .join(' · ')}
        </p>
        {since !== panamaToday(new Date(version.changed_at)) && (
          <p className="text-sm text-slate-500">Guardada el {savedOn}</p>
        )}
        <p className="mt-2 line-clamp-3 text-sm whitespace-pre-line text-slate-800">
          {version.change_summary}
        </p>
        <p className="mt-1 text-sm text-slate-500">{counts.join(' · ')}</p>
      </div>
      <ChevronRightIcon className="mt-1 size-5 shrink-0 text-slate-400" aria-hidden="true" />
    </Link>
  )
}

const actionClass =
  'inline-flex min-h-11 items-center gap-1 rounded-xl px-3 text-sm font-medium ring-1 disabled:opacity-60'

/**
 * A change reconstructed from the bitácora (`med_events`), with its review
 * actions: «Confirmar» (pending only), «Editar» (sheet) and «Descartar».
 */
function EventItem({ event, patientId }) {
  const pending = event.review_status === 'pending_review'
  const update = useUpdateMedEvent(patientId)
  const remove = useDeleteMedEvent(patientId)
  const [editing, setEditing] = useState(false)
  const [discarding, setDiscarding] = useState(false)
  const busy = update.isPending || remove.isPending

  async function discard() {
    try {
      await remove.mutateAsync(event.id)
      setDiscarding(false)
    } catch {
      // the error shows in the dialog
    }
  }

  return (
    <div className="rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-4">
      <div className="flex flex-wrap items-center gap-2 text-xs font-semibold">
        <span className="inline-flex items-center gap-1 rounded-full bg-white px-2 py-0.5 text-slate-700 ring-1 ring-slate-300">
          <BookOpenIcon className="size-3.5" aria-hidden="true" />
          De la bitácora
        </span>
        {pending && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-900 ring-1 ring-amber-300">
            Por revisar
          </span>
        )}
      </div>
      <p className="mt-2 text-slate-900">
        <span className="font-semibold">{EVENT_KIND_LABELS[event.kind] ?? event.kind}:</span>{' '}
        {event.medication}
      </p>
      <p className="text-sm text-slate-600">{formatDate(event.event_date)}</p>
      {event.detail && (
        <p className="mt-1 text-sm whitespace-pre-line text-slate-800">{event.detail}</p>
      )}
      {event.source_entry_id && (
        <Link
          to={`/bitacora/${event.source_entry_id}`}
          className="-ml-2 mt-1 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 text-sm font-medium text-brand-700 active:bg-brand-50"
        >
          Ver entrada de la bitácora
          {event.source_entry_date &&
            event.source_entry_date !== event.event_date &&
            ` del ${formatDate(event.source_entry_date)}`}
          <ChevronRightIcon className="size-4" aria-hidden="true" />
        </Link>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {pending && (
          <button
            type="button"
            disabled={busy}
            onClick={() => update.mutate({ id: event.id, data: { review_status: 'ok' } })}
            className={`${actionClass} bg-brand-700 text-white ring-brand-700 active:bg-brand-800`}
          >
            <CheckIcon className="size-4" aria-hidden="true" />
            {update.isPending ? 'Confirmando…' : 'Confirmar'}
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => setEditing(true)}
          className={`${actionClass} bg-white text-slate-700 ring-slate-300 active:bg-slate-100`}
        >
          <PencilSquareIcon className="size-4" aria-hidden="true" />
          Editar
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => setDiscarding(true)}
          className={`${actionClass} bg-white text-red-700 ring-slate-300 active:bg-red-50`}
        >
          <TrashIcon className="size-4" aria-hidden="true" />
          Descartar
        </button>
      </div>
      {update.isError && (
        <p role="alert" className="mt-2 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(update.error, 'No se pudo confirmar el cambio.')}
        </p>
      )}

      {editing && (
        <MedEventSheet event={event} patientId={patientId} onClose={() => setEditing(false)} />
      )}
      {discarding && (
        <ConfirmDialog
          title="¿Descartar este cambio?"
          message={
            <>
              Se quitará «{EVENT_KIND_LABELS[event.kind] ?? event.kind}: {event.medication}» del
              historial. La entrada de la bitácora no cambia.
            </>
          }
          confirmLabel={remove.isPending ? 'Descartando…' : 'Sí, descartar'}
          danger
          busy={remove.isPending}
          error={remove.isError ? errorMessage(remove.error, 'No se pudo descartar.') : ''}
          onConfirm={discard}
          onCancel={() => {
            remove.reset()
            setDiscarding(false)
          }}
        />
      )}
    </div>
  )
}

/**
 * «Por revisar (N)» at the top of a patient's timeline: switch the timeline
 * to the pending events only, and «Confirmar todos» (the ids shown).
 */
function PendingBar({ pendingEvents, patientId, onlyPending, setOnlyPending }) {
  const confirm = useConfirmMedEvents(patientId)
  const [asking, setAsking] = useState(false)
  const count = pendingEvents.length

  async function confirmAll() {
    try {
      await confirm.mutateAsync(pendingEvents.map((e) => e.id))
      setAsking(false)
      setOnlyPending(false)
    } catch {
      // the error shows in the dialog
    }
  }

  return (
    <div className="mb-4 rounded-2xl bg-amber-50 p-4 ring-1 ring-amber-200">
      <p className="font-semibold text-amber-950">Por revisar ({count})</p>
      <p className="mt-1 text-sm text-amber-900">
        {count === 1
          ? 'Un cambio sacado de la bitácora espera que alguien lo revise.'
          : `${count} cambios sacados de la bitácora esperan que alguien los revise.`}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          aria-pressed={onlyPending}
          onClick={() => setOnlyPending(!onlyPending)}
          className={`${actionClass} bg-white text-slate-800 ring-amber-300 active:bg-amber-100`}
        >
          {onlyPending ? 'Ver todo el historial' : 'Ver solo los por revisar'}
        </button>
        <button
          type="button"
          onClick={() => setAsking(true)}
          className={`${actionClass} bg-brand-700 text-white ring-brand-700 active:bg-brand-800`}
        >
          <CheckIcon className="size-4" aria-hidden="true" />
          Confirmar todos
        </button>
      </div>
      {asking && (
        <ConfirmDialog
          title={`¿Confirmar ${plural(count, 'cambio', 'cambios')}?`}
          message={`Se marcarán como revisados ${
            count === 1 ? 'el cambio' : `los ${count} cambios`
          } de la bitácora por revisar. Puedes seguir editándolos o descartándolos después.`}
          confirmLabel={confirm.isPending ? 'Confirmando…' : 'Sí, confirmar todos'}
          busy={confirm.isPending}
          error={confirm.isError ? errorMessage(confirm.error, 'No se pudo confirmar.') : ''}
          onConfirm={confirmAll}
          onCancel={() => {
            confirm.reset()
            setAsking(false)
          }}
        />
      )}
    </div>
  )
}

/**
 * While older event pages remain, versions older than the oldest loaded event
 * wait for «Cargar más», so each page extends the timeline at its bottom
 * instead of slipping events in between versions already shown.
 */
function visibleVersions(versions = [], loadedEvents, hasMore) {
  if (!hasMore || loadedEvents.length === 0) return versions
  const oldest = loadedEvents[loadedEvents.length - 1].event_date
  return versions.filter((v) => (effectiveDate(v) ?? '') >= oldest)
}

/**
 * One patient's timeline: list versions + bitácora events, newest first, by
 * month. Events come a page at a time («Cargar más»); «Por revisar» uses the
 * full pending list, which paging never hides.
 */
function PatientHistory({ patient, showName }) {
  const versions = useMedVersions(patient.id)
  const events = useMedEvents(patient.id)
  const pending = usePendingMedEvents(patient.id)
  // A failed «Cargar más» keeps the loaded pages (message by the button).
  const failed =
    [versions, events, pending].find((q) => q.isError && !q.isFetchNextPageError) ?? null
  const [onlyPending, setOnlyPending] = useState(false)
  const pendingEvents = pending.data ?? []
  const showPendingOnly = onlyPending && pendingEvents.length > 0
  const loadedEvents = events.data?.pages.flatMap((page) => page.items) ?? []

  let content
  if (versions.isPending || events.isPending || pending.isPending)
    content = <CardSkeletons count={3} />
  else if (failed)
    content = (
      <ErrorState
        message={errorMessage(failed.error, 'No se pudo cargar el historial.')}
        onRetry={() => failed.refetch()}
      />
    )
  else {
    const groups = showPendingOnly
      ? buildTimeline([], pendingEvents)
      : buildTimeline(
          visibleVersions(versions.data, loadedEvents, events.hasNextPage),
          loadedEvents,
        )
    const loadMore = !showPendingOnly && events.hasNextPage
    content = groups.length ? (
      <div className="space-y-6">
        {pendingEvents.length > 0 && (
          <PendingBar
            pendingEvents={pendingEvents}
            patientId={patient.id}
            onlyPending={showPendingOnly}
            setOnlyPending={setOnlyPending}
          />
        )}
        {groups.map((group) => (
          <section key={group.key} aria-label={monthLabel(group.key)}>
            <h3 className="mb-2 text-sm font-semibold tracking-wide text-slate-500 uppercase">
              {monthLabel(group.key)}
            </h3>
            <ol className="space-y-3 border-l-2 border-slate-200 pl-4">
              {group.items.map((item) => (
                <li key={`${item.type}-${item.data.version_no ?? item.data.id}`}>
                  {item.type === 'version' ? (
                    <VersionItem version={item.data} patientId={patient.id} />
                  ) : (
                    <EventItem event={item.data} patientId={patient.id} />
                  )}
                </li>
              ))}
            </ol>
          </section>
        ))}
        {loadMore && (
          <button
            type="button"
            onClick={() => events.fetchNextPage()}
            disabled={events.isFetchingNextPage}
            className="min-h-12 w-full rounded-2xl bg-white font-medium text-brand-700 ring-1 ring-slate-200 active:bg-slate-50 disabled:opacity-60"
          >
            {events.isFetchingNextPage ? 'Cargando…' : 'Cargar más'}
          </button>
        )}
        {!showPendingOnly && events.isFetchNextPageError && (
          <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
            {errorMessage(events.error, 'No se pudieron cargar más cambios.')}
          </p>
        )}
      </div>
    ) : (
      <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
        <ClockIcon className="mx-auto size-10 text-slate-400" aria-hidden="true" />
        <p className="mt-3 font-medium text-slate-800">Todavía no hay historial.</p>
        <p className="mt-1 text-sm text-slate-500">
          Cada vez que se guarda la lista de medicamentos queda una versión aquí.
        </p>
      </div>
    )
  }

  return (
    <section aria-label={`Historial de ${patient.display_name}`}>
      {showName && <h2 className="mb-3 text-xl font-semibold">{patient.display_name}</h2>}
      {content}
    </section>
  )
}

/**
 * Historial (DESIGN §6.4): each selected patient's list versions merged with
 * the changes reconstructed from the bitácora («De la bitácora»), newest
 * first and grouped by month. «Ambos» stacks both patients' timelines, like
 * the Medicamentos tab. A version opens its diff.
 */
export default function MedHistorialPage() {
  const { selectedPatients, patient, isLoading, error } = usePatient()
  return (
    <section className="mx-auto max-w-3xl">
      <Link
        to="/medicamentos"
        className="-ml-2 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 font-medium text-brand-700 active:bg-brand-50"
      >
        <ArrowLeftIcon className="size-5 shrink-0" aria-hidden="true" />
        Medicamentos
      </Link>
      <h1 className="mt-1 mb-4 text-2xl font-semibold">Historial de medicamentos</h1>
      {isLoading && <CardSkeletons count={3} />}
      {error && (
        <ErrorState message={errorMessage(error, 'No se pudieron cargar los pacientes.')} />
      )}
      <div className="space-y-10">
        {selectedPatients.map((p) => (
          <PatientHistory key={p.id} patient={p} showName={patient === 'ambos'} />
        ))}
      </div>
    </section>
  )
}
