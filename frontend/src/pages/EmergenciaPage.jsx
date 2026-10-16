import { useState } from 'react'
import { ArrowDownTrayIcon, ExclamationTriangleIcon, PhoneIcon } from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { errorMessage } from '../lib/bitacora.js'
import { telHref } from '../lib/doctors.js'
import { fetchEmergencyCardPdf, useEmergencyCard } from '../lib/summaries.js'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import PdfButton from '../components/ui/PdfButton.jsx'

const LONG_DATE = new Intl.DateTimeFormat('es', {
  day: 'numeric',
  month: 'long',
  year: 'numeric',
  timeZone: 'UTC',
})

/** '1940-06-02' -> '2 de junio de 1940' (no weekday, no timezone shift). */
function longDate(iso) {
  const [y, m, d] = iso.split('-').map(Number)
  return LONG_DATE.format(new Date(Date.UTC(y, m - 1, d)))
}

function Block({ title, children }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4">
      <h2 className="mb-2 text-sm font-semibold tracking-wide text-slate-500 uppercase">{title}</h2>
      {children}
    </section>
  )
}

function Card({ card }) {
  const name = card.full_name || card.display_name
  return (
    <div className="space-y-4">
      <header className="rounded-2xl border border-slate-200 bg-white p-4">
        <p className="text-2xl font-bold break-words">{name}</p>
        {card.id_number && (
          <p className="mt-1 text-xl">
            CIP: <span className="font-bold tracking-wide">{card.id_number}</span>
          </p>
        )}
        {card.birth_date && (
          <p className="mt-1 text-lg text-slate-700">
            Nacimiento: {longDate(card.birth_date)}
            {card.age !== null && ` (${card.age} años)`}
          </p>
        )}
      </header>

      <section
        aria-labelledby="evitar"
        className="rounded-2xl border-4 border-red-700 bg-red-50 p-4 text-red-950"
      >
        <h2 id="evitar" className="flex items-center gap-2 text-2xl font-bold tracking-wider">
          <ExclamationTriangleIcon className="size-7" aria-hidden="true" />
          EVITAR
        </h2>
        {card.avoid.length > 0 ? (
          <ul className="mt-2 space-y-1 text-lg">
            {card.avoid.map((a) => (
              <li key={a.substance}>
                <span className="font-bold">{a.substance}</span>
                {a.reason && <span> — {a.reason}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-lg">Nada registrado en la lista EVITAR.</p>
        )}
      </section>

      <Block title="Diagnósticos">
        {card.diagnoses ? (
          <p className="text-lg whitespace-pre-line text-slate-900">{card.diagnoses}</p>
        ) : (
          <p className="text-slate-600">No registrados.</p>
        )}
      </Block>

      <Block title="Médicos">
        {card.doctors.length > 0 ? (
          <ul className="divide-y divide-slate-100">
            {card.doctors.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <span className="min-w-0">
                  <span className="block font-semibold break-words">{d.name}</span>
                  {d.specialty && <span className="block text-slate-600">{d.specialty}</span>}
                </span>
                {d.phone && (
                  <a
                    href={telHref(d.phone)}
                    className="flex min-h-12 items-center gap-2 rounded-2xl bg-brand-700 px-4 text-lg font-semibold text-white active:bg-brand-800"
                  >
                    <PhoneIcon className="size-5" aria-hidden="true" />
                    <span className="sr-only">Llamar a {d.name}: </span>
                    {d.phone}
                  </a>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-slate-600">No hay médicos registrados.</p>
        )}
      </Block>

      <Block title="Medicamentos actuales">
        {card.med_groups.length > 0 ? (
          <ul className="divide-y divide-slate-100">
            {card.med_groups.map((m, i) => (
              <li key={`${m.name}-${i}`} className="py-2">
                <span className="text-lg font-semibold">{m.name}</span>
                {m.doses.length > 0 && (
                  <ul className="text-slate-700">
                    {m.doses.map((dose, j) => (
                      <li key={j}>{dose}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-slate-600">No hay medicamentos activos.</p>
        )}
      </Block>
    </div>
  )
}

/**
 * «Tarjeta de emergencia» (DESIGN §6.6, ticket #34): the printed card's data
 * on the phone — big type, EVITAR first, tap-to-call doctors — and
 * «Descargar PDF». Needs a connection: /api is never cached offline.
 */
export default function EmergenciaPage() {
  const { patient, patients, isLoading } = usePatient()
  const [chosen, setChosen] = useState(null)
  const slug = chosen ?? (patient !== 'ambos' ? patient : patients[0]?.slug)
  const current = patients.find((p) => p.slug === slug)
  const query = useEmergencyCard(current?.id)

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Tarjeta de emergencia</h1>

      {patients.length > 1 && (
        <div role="group" aria-label="Paciente" className="flex gap-2">
          {patients.map((p) => (
            <button
              key={p.id}
              type="button"
              aria-pressed={p.slug === slug}
              onClick={() => setChosen(p.slug)}
              className={`min-h-12 flex-1 rounded-2xl px-4 text-lg font-semibold ring-1 sm:flex-none ${
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

      {isLoading || (current && query.isPending) ? (
        <CardSkeletons count={3} />
      ) : !current ? (
        <ErrorState message="No se pudo cargar la lista de pacientes." />
      ) : query.isError ? (
        <ErrorState
          message={errorMessage(query.error, 'No se pudo cargar la tarjeta.')}
          onRetry={() => query.refetch()}
        />
      ) : (
        <>
          <Card card={query.data} />
          <div className="space-y-2 rounded-2xl border border-slate-200 bg-white p-4">
            <p className="text-slate-700">
              Actualizada el {longDate(query.data.updated_on)}. Esta pantalla necesita
              conexión: descarga el PDF para tenerla sin internet o imprimirla.
            </p>
            <PdfButton
              fetchFile={({ signal }) => fetchEmergencyCardPdf(current.id, { signal })}
              label="Descargar PDF"
              shareTitle="Tarjeta de emergencia"
              icon={ArrowDownTrayIcon}
              resetKey={current.id}
            />
          </div>
        </>
      )}
    </div>
  )
}
