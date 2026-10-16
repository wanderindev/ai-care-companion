import { lazy, Suspense, useState } from 'react'
import { Link, useBlocker } from 'react-router-dom'
import { ClipboardDocumentListIcon, ClockIcon, PencilSquareIcon } from '@heroicons/react/24/outline'
import { XMarkIcon } from '@heroicons/react/20/solid'
import { usePatient } from '../contexts/PatientContext.js'
import { panamaToday } from '../lib/appointments.js'
import { errorMessage } from '../lib/bitacora.js'
import { sessionDirty, startSession, useMeds, useMedVersions } from '../lib/meds.js'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'
import MedListView from '../components/meds/MedListView.jsx'
import OutdatedBanner from '../components/meds/OutdatedBanner.jsx'
import PrintButton from '../components/meds/PrintButton.jsx'

// Split out: only loaded when someone edits.
const MedListEditor = lazy(() => import('../components/meds/MedListEditor.jsx'))

function EmptyList({ onCreate }) {
  return (
    <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
      <ClipboardDocumentListIcon className="mx-auto size-10 text-slate-400" aria-hidden="true" />
      <p className="mt-3 font-medium text-slate-800">Todavía no hay lista de medicamentos.</p>
      <p className="mt-1 text-sm text-slate-500">
        Se puede cargar desde la lista impresa (documento de Word) o empezarla aquí, medicamento por
        medicamento.
      </p>
      <button
        type="button"
        onClick={onCreate}
        className="mt-5 min-h-12 rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
      >
        Empezar la lista
      </button>
    </div>
  )
}

/** One patient's list: read-only view, or the editor while a session is open. */
function PatientMeds({ patient, showName, session, onSession, today }) {
  const query = useMeds(patient.id)
  const list = query.data
  const versions = useMedVersions(patient.id, { enabled: Boolean(list?.version_no) })
  const [lost, setLost] = useState(false) // a 409 sent the user back to the current list
  const authorName = versions.data?.find((v) => v.version_no === list?.version_no)?.changed_by_name
  const isEmpty =
    list &&
    list.version_no === 0 &&
    list.avoid.length === 0 &&
    list.slots.every((s) => !s.items.length)

  function startEditing() {
    setLost(false)
    onSession(startSession(list))
  }

  let content
  if (query.isPending) content = <CardSkeletons count={3} />
  else if (query.isError)
    content = (
      <ErrorState
        message={errorMessage(query.error, 'No se pudo cargar la lista de medicamentos.')}
        onRetry={() => query.refetch()}
      />
    )
  else if (session)
    content = (
      <Suspense fallback={<CardSkeletons count={3} />}>
        <MedListEditor
          patientId={patient.id}
          patientName={patient.display_name}
          session={session}
          today={today}
          onChange={onSession}
          onExit={() => onSession(null)}
          onSaved={() => onSession(null)}
          onConflict={() => {
            onSession(null)
            setLost(true)
            query.refetch()
          }}
        />
      </Suspense>
    )
  else if (isEmpty) content = <EmptyList onCreate={startEditing} />
  else
    content = (
      <MedListView
        list={list}
        authorName={authorName}
        today={today}
        patientName={showName ? patient.display_name : undefined}
      />
    )

  return (
    <section aria-label={`Medicamentos de ${patient.display_name}`}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        {showName ? <h2 className="text-xl font-semibold">{patient.display_name}</h2> : <span />}
        {list && !session && !isEmpty && (
          <div className="flex flex-wrap items-start gap-2">
            {/* Keyed by version: a save makes a prepared PDF stale. */}
            <PrintButton
              key={list.version_no}
              patientId={patient.id}
              ariaLabel={
                showName ? `Imprimir o descargar la lista de ${patient.display_name}` : undefined
              }
            />
            <button
              type="button"
              onClick={startEditing}
              className="flex min-h-12 shrink-0 items-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800"
            >
              <PencilSquareIcon className="size-5" aria-hidden="true" />
              {showName ? `Editar lista de ${patient.display_name}` : 'Editar lista'}
            </button>
          </div>
        )}
      </div>
      {lost && !session && (
        <div
          role="status"
          className="mb-4 flex items-start gap-2 rounded-2xl border border-amber-300 bg-amber-50 p-3 text-amber-950"
        >
          <p className="flex-1 text-sm">
            Esta es la lista actual, con lo que guardó la otra persona. Tus cambios no se guardaron:
            vuelve a hacerlos con «Editar lista».
          </p>
          <button
            type="button"
            onClick={() => setLost(false)}
            aria-label="Cerrar aviso"
            className="flex size-11 shrink-0 items-center justify-center rounded-xl active:bg-amber-100"
          >
            <XMarkIcon className="size-5" aria-hidden="true" />
          </button>
        </div>
      )}
      {list && <OutdatedBanner patientId={patient.id} versionNo={list.version_no} />}
      {content}
    </section>
  )
}

/**
 * Medicamentos tab (DESIGN §6.4): each selected patient's current list,
 * grouped by slot like the printed sheet, with EVITAR. «Ambos» stacks both
 * lists, each with its own «Editar». Edit sessions live here, keyed by
 * patient, so switching patient keeps unsaved work; leaving the tab with
 * unsaved changes asks first.
 */
export default function MedicamentosPage() {
  const { selectedPatients, patient, isLoading, error } = usePatient()
  const [sessions, setSessions] = useState({}) // patientId -> edit session
  const today = panamaToday()
  const dirty = Object.values(sessions).some(sessionDirty)
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && currentLocation.pathname !== nextLocation.pathname,
  )

  const setSession = (patientId) => (session) =>
    setSessions((all) => {
      const next = { ...all }
      if (session) next[patientId] = session
      else delete next[patientId]
      return next
    })

  return (
    // The printed sheet is one column: keep it readable on wide screens too.
    <section className="mx-auto max-w-3xl">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Medicamentos</h1>
        <Link
          to="/medicamentos/historial"
          className="flex min-h-12 shrink-0 items-center gap-2 rounded-2xl px-3 font-medium text-brand-700 ring-1 ring-slate-300 active:bg-brand-50"
        >
          <ClockIcon className="size-5" aria-hidden="true" />
          Historial
        </Link>
      </div>
      {isLoading && <CardSkeletons count={3} />}
      {error && (
        <ErrorState message={errorMessage(error, 'No se pudieron cargar los pacientes.')} />
      )}
      <div className="space-y-10">
        {selectedPatients.map((p) => (
          <PatientMeds
            key={p.id}
            patient={p}
            showName={patient === 'ambos'}
            session={sessions[p.id] ?? null}
            onSession={setSession(p.id)}
            today={today}
          />
        ))}
      </div>
      {blocker.state === 'blocked' && (
        <ConfirmDialog
          title="¿Salir sin guardar?"
          message="Tienes cambios en la lista de medicamentos que no se han guardado. Si sales, se pierden."
          confirmLabel="Salir sin guardar"
          cancelLabel="Seguir editando"
          danger
          onConfirm={() => blocker.proceed()}
          onCancel={() => blocker.reset()}
        />
      )}
    </section>
  )
}
