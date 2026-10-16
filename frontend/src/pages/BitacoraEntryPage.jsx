import { useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeftIcon,
  CheckCircleIcon,
  PencilSquareIcon,
  TrashIcon,
} from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { useDeleteLogEntry, useLogEntry, useMarkReviewed } from '../lib/logEntries.js'
import {
  APPOINTMENT_STATUS_LABELS,
  appointmentLabel,
  channelLabel,
  entrySource,
  errorMessage,
  formatDate,
  formatLongDate,
  formatTime,
} from '../lib/bitacora.js'
import { ReviewBadge, TypeChip } from '../components/bitacora/EntryBadges.jsx'
import Markdown from '../components/bitacora/Markdown.jsx'
import { AttachmentList } from '../components/bitacora/Attachments.jsx'
import EntrySheet from '../components/bitacora/EntrySheet.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'
import { ErrorState } from '../components/bitacora/ListStates.jsx'

function DetailSkeleton() {
  return (
    <div aria-hidden="true" className="animate-pulse space-y-4">
      <div className="h-5 w-24 rounded bg-slate-200" />
      <div className="h-7 w-2/3 rounded bg-slate-200" />
      <div className="h-4 w-1/2 rounded bg-slate-100" />
      <div className="h-32 rounded-2xl bg-slate-100" />
    </div>
  )
}

function Section({ title, children }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4">
      <h2 className="mb-2 text-sm font-semibold tracking-wide text-slate-500 uppercase">{title}</h2>
      {children}
    </section>
  )
}

/** One bitácora entry: full text, attachments, edit / review / delete. */
export default function BitacoraEntryPage() {
  const { entryId } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const { patients } = usePatient()
  const entryQuery = useLogEntry(entryId)
  const markReviewed = useMarkReviewed()
  const remove = useDeleteLogEntry()
  const [editing, setEditing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  // Opened from a doctor's page: back (and after delete) returns there.
  const backTo = /^\/(?!\/)/.test(location.state?.backTo ?? '') ? location.state.backTo : null
  const listUrl = backTo ?? `/bitacora${location.state?.from ?? ''}`
  const entry = entryQuery.data

  const backLink = (
    <Link
      to={listUrl}
      className="-ml-2 inline-flex min-h-11 max-w-full items-center gap-1 rounded-xl px-2 font-medium text-brand-700 active:bg-brand-50"
    >
      <ArrowLeftIcon className="size-5 shrink-0" aria-hidden="true" />
      <span className="truncate">{backTo ? (location.state.backLabel ?? 'Volver') : 'Bitácora'}</span>
    </Link>
  )

  if (entryQuery.isPending) {
    return (
      <div>
        {backLink}
        <div className="mt-3">
          <DetailSkeleton />
        </div>
      </div>
    )
  }
  if (entryQuery.isError) {
    const notFound = entryQuery.error.status === 404 || entryQuery.error.status === 422
    return (
      <div>
        {backLink}
        <div className="mt-3">
          <ErrorState
            message={
              notFound
                ? 'Esta entrada no existe o fue eliminada.'
                : errorMessage(entryQuery.error, 'No se pudo cargar la entrada.')
            }
            onRetry={notFound ? undefined : () => entryQuery.refetch()}
          />
        </div>
      </div>
    )
  }

  const patientName = patients.find((p) => p.id === entry.patient_id)?.display_name
  const source = entrySource(entry)
  const pending = entry.review_status === 'pending_review'
  const heading = entry.title || source || `Entrada del ${formatDate(entry.entry_date)}`
  const actionClass =
    'flex min-h-12 flex-1 items-center justify-center gap-2 rounded-2xl px-4 font-medium sm:flex-none'

  async function handleDelete() {
    try {
      await remove.mutateAsync(entry.id)
      navigate(listUrl, { replace: true })
    } catch {
      // Shown in the dialog.
    }
  }

  return (
    <article className="space-y-4">
      {backLink}

      <header>
        <div className="flex flex-wrap items-center gap-2">
          <TypeChip type={entry.entry_type} />
          {pending && <ReviewBadge />}
          {patientName && (
            <span className="text-sm font-semibold text-brand-800">{patientName}</span>
          )}
        </div>
        <h1 className="mt-2 text-2xl font-semibold break-words">{heading}</h1>
        <p className="mt-1 text-slate-600 first-letter:uppercase">
          <time dateTime={entry.entry_date}>{formatLongDate(entry.entry_date)}</time>
          {entry.entry_time && ` · ${formatTime(entry.entry_time)}`}
        </p>
        <dl className="mt-2 space-y-0.5 text-sm text-slate-700">
          {entry.doctor && (
            <div className="flex gap-1">
              <dt className="text-slate-500">Médico:</dt>
              <dd className="font-medium">{entry.doctor.name}</dd>
            </div>
          )}
          {entry.source_label && (
            <div className="flex gap-1">
              <dt className="text-slate-500">Fuente:</dt>
              <dd>{entry.source_label}</dd>
            </div>
          )}
          {entry.channel && (
            <div className="flex gap-1">
              <dt className="text-slate-500">Canal:</dt>
              <dd>{channelLabel(entry.channel)}</dd>
            </div>
          )}
          {entry.next_appointment && (
            <div className="flex flex-wrap gap-x-1">
              <dt className="text-slate-500">Próxima cita:</dt>
              <dd>
                {appointmentLabel(entry.next_appointment)}
                {entry.next_appointment.doctor && ` · ${entry.next_appointment.doctor.name}`}
                {entry.next_appointment.status !== 'programada' &&
                  ` (${APPOINTMENT_STATUS_LABELS[entry.next_appointment.status].toLowerCase()})`}
              </dd>
            </div>
          )}
        </dl>
      </header>

      {pending && (
        <div className="rounded-2xl border border-amber-300 bg-amber-50 p-4">
          <p className="font-medium text-amber-950">Esta entrada está por revisar.</p>
          <p className="mt-1 text-sm text-amber-900">
            Se creó automáticamente (por ejemplo, al importar el documento) y nadie la ha
            confirmado. Revisa que el texto esté bien y márcala como revisada.
          </p>
          {markReviewed.isError && (
            <p role="alert" className="mt-2 text-sm text-red-800">
              {errorMessage(markReviewed.error, 'No se pudo marcar como revisada.')}
            </p>
          )}
          <button
            type="button"
            onClick={() => markReviewed.mutate(entry.id)}
            disabled={markReviewed.isPending}
            className="mt-3 flex min-h-12 items-center gap-2 rounded-2xl bg-amber-900 px-4 font-semibold text-white active:bg-amber-950 disabled:opacity-60"
          >
            <CheckCircleIcon className="size-5" aria-hidden="true" />
            {markReviewed.isPending ? 'Guardando…' : 'Marcar como revisado'}
          </button>
        </div>
      )}

      {entry.indications && (
        <Section title="Indicaciones">
          <Markdown>{entry.indications}</Markdown>
        </Section>
      )}
      {entry.family_notes && (
        <Section title="Notas de la familia">
          <Markdown>{entry.family_notes}</Markdown>
        </Section>
      )}
      {!entry.indications && !entry.family_notes && (
        <p className="text-slate-500">Sin indicaciones ni notas.</p>
      )}

      {entry.attachments.length > 0 && (
        <Section title={`Adjuntos (${entry.attachments.length})`}>
          <AttachmentList attachments={entry.attachments} />
        </Section>
      )}

      <div className="flex flex-wrap gap-2 pt-2">
        <button
          type="button"
          onClick={() => setEditing(true)}
          className={`${actionClass} bg-brand-700 text-white active:bg-brand-800`}
        >
          <PencilSquareIcon className="size-5" aria-hidden="true" />
          Editar
        </button>
        <button
          type="button"
          onClick={() => {
            remove.reset()
            setConfirmDelete(true)
          }}
          className={`${actionClass} text-red-700 ring-1 ring-red-200 active:bg-red-50`}
        >
          <TrashIcon className="size-5" aria-hidden="true" />
          Eliminar
        </button>
      </div>

      {editing && (
        <EntrySheet
          entry={entry}
          onClose={() => setEditing(false)}
          onSaved={() => setEditing(false)}
        />
      )}
      {confirmDelete && (
        <ConfirmDialog
          title="¿Eliminar esta entrada?"
          message={
            <>
              Se borrará de la bitácora para toda la familia. <strong>No se puede deshacer.</strong>
            </>
          }
          confirmLabel={remove.isPending ? 'Eliminando…' : 'Eliminar'}
          danger
          busy={remove.isPending}
          error={remove.isError ? errorMessage(remove.error, 'No se pudo eliminar.') : ''}
          onConfirm={handleDelete}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
    </article>
  )
}
