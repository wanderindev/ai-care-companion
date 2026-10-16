import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeftIcon } from '@heroicons/react/24/outline'
import { ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/20/solid'
import { usePatient } from '../contexts/PatientContext.js'
import { panamaToday } from '../lib/appointments.js'
import { errorMessage, formatDate } from '../lib/bitacora.js'
import { effectiveDate, SLOTS, useMedVersion, versionToList } from '../lib/meds.js'
import { leddChangeText } from '../lib/ledd.js'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import MedDiff from '../components/meds/MedDiff.jsx'
import MedListView from '../components/meds/MedListView.jsx'
import PrintButton from '../components/meds/PrintButton.jsx'

function positiveInt(value) {
  const n = Number(value)
  return Number.isInteger(n) && n > 0 ? n : null
}

const navClass =
  'flex min-h-12 flex-1 items-center justify-center gap-1 rounded-2xl bg-white px-3 font-medium text-brand-700 ring-1 ring-slate-200 active:bg-slate-50'

/** «Versión anterior» / «Versión siguiente». */
function VersionNav({ version, patientId }) {
  const to = (n) => `/medicamentos/historial/${n}?paciente=${patientId}`
  if (!version.previous_version_no && !version.next_version_no) return null
  return (
    <nav aria-label="Otras versiones" className="flex gap-3">
      {version.previous_version_no ? (
        <Link to={to(version.previous_version_no)} className={navClass}>
          <ChevronLeftIcon className="size-5 shrink-0" aria-hidden="true" />
          Versión {version.previous_version_no}
        </Link>
      ) : (
        <span className="flex-1" />
      )}
      {version.next_version_no ? (
        <Link to={to(version.next_version_no)} className={navClass}>
          Versión {version.next_version_no}
          <ChevronRightIcon className="size-5 shrink-0" aria-hidden="true" />
        </Link>
      ) : (
        <span className="flex-1" />
      )}
    </nav>
  )
}

/**
 * One version of a patient's list (`/medicamentos/historial/:versionNo?paciente=<id>`):
 * header (Desde, saved when / by whom, summary), the diff vs the previous
 * version with words for each change, and «Ver lista completa de esta
 * versión» (the snapshot as the printed sheet). Version 1 is the starting
 * list, shown whole. «Imprimir esta versión» prints the snapshot as a PDF.
 */
export default function MedVersionPage() {
  const params = useParams()
  const versionNo = positiveInt(params.versionNo)
  const [searchParams] = useSearchParams()
  const { patients, selectedPatients } = usePatient()
  // From the URL; else the switcher's patient when it shows only one.
  const patientId =
    positiveInt(searchParams.get('paciente')) ??
    (selectedPatients.length === 1 ? selectedPatients[0].id : null)
  const query = useMedVersion(patientId, versionNo)
  // Keyed by version, so moving to another version closes the full list.
  const [openFor, setOpenFor] = useState(null)
  const patientName = patients.find((p) => p.id === patientId)?.display_name

  const backLink = (
    <Link
      to="/medicamentos/historial"
      className="-ml-2 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 font-medium text-brand-700 active:bg-brand-50"
    >
      <ArrowLeftIcon className="size-5 shrink-0" aria-hidden="true" />
      Historial
    </Link>
  )

  if (!patientId || !versionNo) {
    return (
      <section className="mx-auto max-w-3xl space-y-4">
        {backLink}
        <p className="text-slate-700">
          {versionNo
            ? 'Elige a Papá o a Mamá arriba para ver esta versión.'
            : 'Esta versión no existe.'}
        </p>
      </section>
    )
  }

  if (query.isPending || query.isError) {
    const notFound = query.error?.status === 404 || query.error?.status === 422
    return (
      <section className="mx-auto max-w-3xl space-y-3">
        {backLink}
        {query.isPending ? (
          <CardSkeletons count={3} />
        ) : (
          <ErrorState
            message={
              notFound
                ? 'Esta versión no existe.'
                : errorMessage(query.error, 'No se pudo cargar la versión.')
            }
            onRetry={notFound ? undefined : () => query.refetch()}
          />
        )}
      </section>
    )
  }

  const version = query.data
  const list = { ...versionToList(version, patientId), ledd: version.ledd }
  const leddChange = leddChangeText(version.ledd_change)
  const since = effectiveDate(version)
  const savedOn = formatDate(panamaToday(new Date(version.changed_at)))
  const initial = version.previous_version_no == null
  const showFull = initial || openFor === version.version_no

  return (
    <article className="mx-auto max-w-3xl space-y-5">
      {backLink}
      <header className="rounded-2xl bg-white p-4 ring-1 ring-slate-200">
        <p className="text-sm text-slate-600">{patientName}</p>
        <h1 className="text-2xl font-semibold">
          Versión {version.version_no}
          {initial && <span className="font-normal"> · Lista inicial</span>}
        </h1>
        {since && <p className="text-lg font-semibold text-slate-900">Desde {formatDate(since)}</p>}
        <p className="text-sm text-slate-600">
          Guardada el {savedOn}
          {version.changed_by_name && ` por ${version.changed_by_name}`}
        </p>
        <p className="mt-2 text-sm whitespace-pre-line text-slate-800">
          <span className="font-medium">Resumen:</span> {version.change_summary}
        </p>
        {leddChange && !initial && (
          <p className="mt-1 text-sm font-medium text-slate-800">{leddChange}</p>
        )}
        <div className="mt-3">
          <PrintButton
            key={version.version_no}
            patientId={patientId}
            versionNo={version.version_no}
            label="Imprimir esta versión"
          />
        </div>
      </header>

      <VersionNav version={version} patientId={patientId} />

      {!initial && (
        <section
          aria-labelledby="version-diff"
          className="rounded-2xl bg-white p-4 ring-1 ring-slate-200"
        >
          <h2 id="version-diff" className="font-semibold text-slate-900">
            Cambios respecto a la versión {version.previous_version_no}
          </h2>
          <MedDiff diff={version.diff} slots={SLOTS} past />
        </section>
      )}

      {showFull ? (
        <section aria-labelledby="version-list" className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id="version-list" className="text-xl font-semibold">
              {initial ? 'Lista inicial' : `Lista completa de la versión ${version.version_no}`}
            </h2>
            {!initial && (
              <button
                type="button"
                onClick={() => setOpenFor(null)}
                className="min-h-11 rounded-xl px-3 font-medium text-brand-700 active:bg-brand-50"
              >
                Ocultar lista
              </button>
            )}
          </div>
          <MedListView list={list} today={since} showMeta={false} />
        </section>
      ) : (
        <button
          type="button"
          onClick={() => setOpenFor(version.version_no)}
          className="min-h-12 w-full rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
        >
          Ver lista completa de esta versión
        </button>
      )}
    </article>
  )
}
