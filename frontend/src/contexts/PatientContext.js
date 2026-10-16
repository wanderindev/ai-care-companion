import { createContext, useContext } from 'react'

/**
 * Patient filter values. Slugs match `patients.slug` (docs/DESIGN.md §5.1);
 * the labels are fallbacks until GET /patients answers with display names.
 */
export const PATIENT_OPTIONS = [
  { value: 'papa', label: 'Papá' },
  { value: 'mama', label: 'Mamá' },
  { value: 'ambos', label: 'Ambos' },
]

export const DEFAULT_PATIENT = 'ambos'

/**
 * - `patient`: the switcher value, 'papa' | 'mama' | 'ambos'
 * - `patients`: both patient records from the API ([] while loading)
 * - `selectedPatients` / `patientIds`: the records (ids) the current view
 *   should show — both for 'ambos'. Use `patientIds` for API filters.
 */
export const PatientContext = createContext({
  patient: DEFAULT_PATIENT,
  setPatient: () => {},
  patients: [],
  selectedPatients: [],
  patientIds: [],
  isLoading: false,
  error: null,
})

export function usePatient() {
  return useContext(PatientContext)
}
