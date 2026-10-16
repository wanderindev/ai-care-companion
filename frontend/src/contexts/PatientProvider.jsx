import { useCallback, useMemo, useState } from 'react'
import { usePatientsQuery } from '../lib/patients.js'
import { DEFAULT_PATIENT, PATIENT_OPTIONS, PatientContext } from './PatientContext.js'

// The selection is remembered per device (docs/DESIGN.md §3).
const STORAGE_KEY = 'n16chat.patient'
const VALID = new Set(PATIENT_OPTIONS.map((o) => o.value))
const NO_PATIENTS = []

function readStored() {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return VALID.has(value) ? value : DEFAULT_PATIENT
  } catch {
    return DEFAULT_PATIENT
  }
}

/** Mounted inside the signed-in layout: it loads GET /patients. */
export function PatientProvider({ children }) {
  const [patient, setPatientState] = useState(readStored)
  const query = usePatientsQuery()
  const patients = query.data ?? NO_PATIENTS

  const setPatient = useCallback((value) => {
    if (!VALID.has(value)) return
    setPatientState(value)
    try {
      window.localStorage.setItem(STORAGE_KEY, value)
    } catch {
      // Storage unavailable (private mode): the choice lasts for this session only.
    }
  }, [])

  const value = useMemo(() => {
    const selectedPatients =
      patient === 'ambos' ? patients : patients.filter((p) => p.slug === patient)
    return {
      patient,
      setPatient,
      patients,
      selectedPatients,
      patientIds: selectedPatients.map((p) => p.id),
      isLoading: query.isPending,
      error: query.error,
    }
  }, [patient, setPatient, patients, query.isPending, query.error])

  return <PatientContext.Provider value={value}>{children}</PatientContext.Provider>
}
