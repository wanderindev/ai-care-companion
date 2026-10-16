import { PATIENT_OPTIONS, usePatient } from '../contexts/PatientContext.js'

/** Temporary page body until each section is built. */
export default function PlaceholderPage({ title, description }) {
  const { patient } = usePatient()
  const patientLabel = PATIENT_OPTIONS.find((o) => o.value === patient)?.label

  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-1 text-sm text-slate-500">Mostrando: {patientLabel}</p>
      <div className="mt-6 rounded-2xl border border-dashed border-slate-300 bg-white p-6 text-center">
        <p className="text-slate-700">{description}</p>
        <p className="mt-2 text-sm text-slate-500">Próximamente.</p>
      </div>
    </section>
  )
}
