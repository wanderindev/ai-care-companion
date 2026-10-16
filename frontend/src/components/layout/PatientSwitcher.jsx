import { PATIENT_OPTIONS, usePatient } from '../../contexts/PatientContext.js'

/** Segmented control: Papá / Mamá / Ambos (labels from GET /patients when loaded). */
export default function PatientSwitcher() {
  const { patient, setPatient, patients } = usePatient()
  const names = Object.fromEntries(patients.map((p) => [p.slug, p.display_name]))

  return (
    <div role="radiogroup" aria-label="Paciente" className="flex rounded-full bg-slate-100 p-1">
      {PATIENT_OPTIONS.map((option) => {
        const selected = option.value === patient
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => setPatient(option.value)}
            className={`min-h-10 rounded-full px-3 text-sm font-medium transition-colors ${
              selected ? 'bg-brand-700 text-white shadow-sm' : 'text-slate-600 active:bg-slate-200'
            }`}
          >
            {names[option.value] ?? option.label}
          </button>
        )
      })}
    </div>
  )
}
