import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeftIcon,
  ArrowsPointingInIcon,
  ClipboardDocumentListIcon,
  CheckCircleIcon,
  EnvelopeIcon,
  MapPinIcon,
  PencilSquareIcon,
  PhoneIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { useMe } from '../lib/auth.js'
import { EMPTY_FILTERS, errorMessage, filtersToSearchParams } from '../lib/bitacora.js'
import { telHref, useDoctors } from '../lib/doctors.js'
import { useLogEntriesInfinite } from '../lib/logEntries.js'
import { usePatientsQuery } from '../lib/patients.js'
import { fetchPrevisitPdf } from '../lib/summaries.js'
import PdfButton from '../components/ui/PdfButton.jsx'
import DoctorSheet from '../components/doctors/DoctorSheet.jsx'
import MergeSheet from '../components/doctors/MergeSheet.jsx'
import InactiveBadge from '../components/doctors/InactiveBadge.jsx'
import EntryCard from '../components/bitacora/EntryCard.jsx'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'

const PAGE_SIZE = 10
const NO_DOCTORS = []

function Section({ title, children }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4">
      <h2 className="mb-2 text-sm font-semibold tracking-wide text-slate-500 uppercase">{title}</h2>
      {children}
    </section>
  )
}

const contactClass =
  'flex min-h-11 items-center gap-3 rounded-xl px-2 -mx-2 text-brand-800 underline-offset-2 active:bg-brand-50'

/**
 * «Resumen pre-consulta» (ticket #34): a PDF of everything since the last
 * visit with this doctor. With «Ambos» selected, the patient is chosen here.
 */
function PrevisitSummary({ doctor }) {
  const { patient, patients } = usePatient()
  const [chosen, setChosen] = useState(null)
  const slug = patient !== 'ambos' ? patient : chosen
  const current = patients.find((p) => p.slug === slug)

  return (
    <Section title="Resumen pre-consulta">
      <p className="mb-3 text-slate-700">
        Un PDF con lo ocurrido desde la última consulta con este médico: bitácora, cambios de
        medicamentos, laboratorios, próximas citas y espacio para preguntas.
      </p>
      {patient === 'ambos' && patients.length > 1 && (
        <div role="group" aria-label="¿De quién?" className="mb-3 flex gap-2">
          {patients.map((p) => (
            <button
              key={p.id}
              type="button"
              aria-pressed={p.slug === slug}
              onClick={() => setChosen(p.slug)}
              className={`min-h-12 flex-1 rounded-2xl px-4 font-semibold ring-1 sm:flex-none ${
                p.slug === slug
                  ? 'bg-brand-700 text-white ring-brand-700'
                  : 'bg-white text-slate-800 ring-slate-300 active:bg-slate-100'
              }`}
            >
              {p.display_name}
            </button>
          ))}
        </div>
      )}
      {current ? (
        <PdfButton
          fetchFile={({ signal }) => fetchPrevisitPdf(current.id, doctor.id, { signal })}
          label={`Resumen pre-consulta de ${current.display_name}`}
          shareTitle="Resumen pre-consulta"
          icon={ClipboardDocumentListIcon}
          resetKey={`${current.id}:${doctor.id}`}
        />
      ) : (
        patients.length > 0 && (
          <p className="text-sm text-slate-600">Elige de quién es la consulta.</p>
        )
      )}
    </Section>
  )
}

/** This doctor's bitácora entries for the selected patient(s). */
function DoctorEntries({ doctor }) {
  const {
    patient,
    patients,
    selectedPatients,
    patientIds,
    isLoading: patientsLoading,
  } = usePatient()
  const patientsQuery = usePatientsQuery()
  const filters = useMemo(() => ({ ...EMPTY_FILTERS, doctorIds: [doctor.id] }), [doctor.id])
  // Never fall back to an unfiltered list while the patient filter is unknown.
  const ready = !patientsLoading && !patientsQuery.isError
  const query = useLogEntriesInfinite(filters, patientIds, { enabled: ready, pageSize: PAGE_SIZE })
  const entries = query.data?.pages.flatMap((page) => page.items) ?? []
  const total = query.data?.pages[0]?.total
  const patientNames =
    patient === 'ambos' ? Object.fromEntries(patients.map((p) => [p.id, p.display_name])) : {}
  const linkState = { backTo: `/medicos/${doctor.id}`, backLabel: doctor.name }
  const whose =
    patient !== 'ambos' && selectedPatients[0] ? ` de ${selectedPatients[0].display_name}` : ''

  return (
    <section aria-labelledby="doctor-entries">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <h2 id="doctor-entries" className="text-lg font-semibold">
          Entradas en la bitácora{whose}
          {total !== undefined && (
            <span className="ml-2 text-base font-normal text-slate-500">({total})</span>
          )}
        </h2>
        <Link
          to={`/bitacora?${filtersToSearchParams(filters)}`}
          className="-ml-2 flex min-h-11 items-center rounded-xl px-2 font-medium text-brand-700 underline sm:ml-0 underline-offset-2 active:bg-brand-50"
        >
          Ver todas en la bitácora
        </Link>
      </div>
      {patientsQuery.isError ? (
        <ErrorState
          message={errorMessage(patientsQuery.error, 'No se pudo cargar la lista de pacientes.')}
          onRetry={() => patientsQuery.refetch()}
        />
      ) : !ready || query.isPending ? (
        <CardSkeletons count={2} />
      ) : query.isError ? (
        <ErrorState
          message={errorMessage(query.error, 'No se pudieron cargar las entradas.')}
          onRetry={() => query.refetch()}
        />
      ) : entries.length === 0 ? (
        <p className="rounded-2xl border border-dashed border-slate-300 bg-white px-4 py-6 text-center text-slate-600">
          No hay entradas de este médico{whose}.
        </p>
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
          {query.hasNextPage && (
            <button
              type="button"
              onClick={() => query.fetchNextPage()}
              disabled={query.isFetchingNextPage}
              className="mt-3 min-h-12 w-full rounded-2xl bg-white font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
            >
              {query.isFetchingNextPage
                ? 'Cargando…'
                : `Cargar más (${entries.length} de ${total})`}
            </button>
          )}
        </>
      )}
    </section>
  )
}

/** One doctor: contact info, aliases, notes, edit, merge (admins) and their entries. */
export default function MedicoPage() {
  const { doctorId } = useParams()
  const { data: doctors = NO_DOCTORS, isPending, isError, error, refetch } = useDoctors()
  const { data: me } = useMe()
  const [editing, setEditing] = useState(false)
  const [merging, setMerging] = useState(false)
  const [notice, setNotice] = useState('')
  const doctor = doctors.find((d) => String(d.id) === doctorId)

  const backLink = (
    <Link
      to="/medicos"
      className="-ml-2 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 font-medium text-brand-700 active:bg-brand-50"
    >
      <ArrowLeftIcon className="size-5" aria-hidden="true" />
      Médicos
    </Link>
  )

  if (isPending) {
    return (
      <div>
        {backLink}
        <div className="mt-3">
          <CardSkeletons count={2} />
        </div>
      </div>
    )
  }
  if (isError || !doctor) {
    return (
      <div>
        {backLink}
        <div className="mt-3">
          <ErrorState
            message={
              isError
                ? errorMessage(error, 'No se pudo cargar el médico.')
                : 'Este médico no existe o se combinó con otro.'
            }
            onRetry={isError ? () => refetch() : undefined}
          />
        </div>
      </div>
    )
  }

  const actionClass =
    'flex min-h-12 flex-1 items-center justify-center gap-2 rounded-2xl px-3 font-medium whitespace-nowrap sm:flex-none sm:px-4'

  return (
    <article className="space-y-4">
      {backLink}

      <header>
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold break-words">{doctor.name}</h1>
          {!doctor.is_active && <InactiveBadge />}
        </div>
        {doctor.specialty && <p className="mt-1 text-slate-700">{doctor.specialty}</p>}
        {doctor.clinic && <p className="text-slate-600">{doctor.clinic}</p>}
      </header>

      {notice && (
        <div
          role="status"
          className="flex items-center gap-2 rounded-2xl border border-brand-100 bg-brand-50 py-1 pr-1 pl-3 text-brand-800"
        >
          <CheckCircleIcon className="size-5 shrink-0" aria-hidden="true" />
          <span className="flex-1 font-medium">{notice}</span>
          <button
            type="button"
            onClick={() => setNotice('')}
            aria-label="Cerrar aviso"
            className="flex size-11 items-center justify-center rounded-xl active:bg-brand-100"
          >
            <XMarkIcon className="size-5" aria-hidden="true" />
          </button>
        </div>
      )}

      {(doctor.phone || doctor.email || doctor.address) && (
        <Section title="Contacto">
          <ul className="space-y-1">
            {doctor.phone && (
              <li>
                <a href={telHref(doctor.phone)} className={contactClass}>
                  <PhoneIcon className="size-5 shrink-0" aria-hidden="true" />
                  <span>
                    <span className="sr-only">Llamar: </span>
                    {doctor.phone}
                  </span>
                </a>
              </li>
            )}
            {doctor.email && (
              <li>
                <a href={`mailto:${doctor.email}`} className={`${contactClass} break-all`}>
                  <EnvelopeIcon className="size-5 shrink-0" aria-hidden="true" />
                  {doctor.email}
                </a>
              </li>
            )}
            {doctor.address && (
              <li className="flex gap-3 py-2 text-slate-700">
                <MapPinIcon className="size-5 shrink-0 text-slate-500" aria-hidden="true" />
                <span className="whitespace-pre-line">{doctor.address}</span>
              </li>
            )}
          </ul>
        </Section>
      )}

      {doctor.aliases.length > 0 && (
        <Section title="También se escribe">
          <ul className="flex flex-wrap gap-2">
            {doctor.aliases.map((alias) => (
              <li key={alias} className="rounded-full bg-slate-100 px-3 py-1 text-slate-800">
                {alias}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {doctor.notes && (
        <Section title="Notas">
          <p className="whitespace-pre-line text-slate-800">{doctor.notes}</p>
        </Section>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => setEditing(true)}
          className={`${actionClass} bg-brand-700 text-white active:bg-brand-800`}
        >
          <PencilSquareIcon className="size-5" aria-hidden="true" />
          Editar
        </button>
        {me?.is_admin && doctor.is_active && (
          <button
            type="button"
            onClick={() => setMerging(true)}
            className={`${actionClass} text-slate-800 ring-1 ring-slate-300 active:bg-slate-100`}
          >
            <ArrowsPointingInIcon className="size-5" aria-hidden="true" />
            Combinar con…
          </button>
        )}
      </div>
      {me?.is_admin && !doctor.is_active && (
        <p className="text-sm text-slate-500">
          Para combinar un duplicado con este médico, primero márcalo como activo.
        </p>
      )}

      <PrevisitSummary doctor={doctor} />

      <DoctorEntries doctor={doctor} />

      {editing && (
        <DoctorSheet
          doctor={doctor}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            setNotice('Médico guardado.')
          }}
        />
      )}
      {merging && (
        <MergeSheet
          target={doctor}
          onClose={() => setMerging(false)}
          onMerged={(_merged, duplicate) => {
            setMerging(false)
            setNotice(`Se combinó «${duplicate.name}» con este médico.`)
          }}
        />
      )}
    </article>
  )
}
