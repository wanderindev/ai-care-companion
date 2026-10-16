import { useId, useState } from 'react'
import Sheet from '../ui/Sheet.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import { usePatient } from '../../contexts/PatientContext.js'
import { useUpdateLabReport } from '../../lib/labReports.js'
import { errorMessage, todayISO } from '../../lib/bitacora.js'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none'

/**
 * Correct a report's patient, dates or lab name. A patient change is
 * confirmed in a dialog before it is saved: it moves every result of the
 * report to another person's history.
 */
export default function ReportEditSheet({ report, onClose, onSaved }) {
  const { patients } = usePatient()
  const update = useUpdateLabReport()
  const formId = useId()
  const [patientId, setPatientId] = useState(report.patient_id)
  const [collectedOn, setCollectedOn] = useState(report.collected_on ?? '')
  const [reportedOn, setReportedOn] = useState(report.reported_on ?? '')
  const [labName, setLabName] = useState(report.lab_name ?? '')
  const [confirming, setConfirming] = useState(false)
  const today = todayISO()

  const changes = {}
  if (patientId !== report.patient_id && patientId !== null) changes.patient_id = patientId
  if ((collectedOn || null) !== report.collected_on) changes.collected_on = collectedOn || null
  if ((reportedOn || null) !== report.reported_on) changes.reported_on = reportedOn || null
  if ((labName.trim() || null) !== report.lab_name) changes.lab_name = labName.trim() || null
  const dirty = Object.keys(changes).length > 0

  const nameOf = (id) => patients.find((p) => p.id === id)?.display_name ?? ''

  async function save() {
    try {
      const saved = await update.mutateAsync({ id: report.id, data: changes })
      onSaved(saved)
    } catch {
      setConfirming(false) // the error shows in the sheet
    }
  }

  function handleSubmit(event) {
    event.preventDefault()
    if (!dirty) return onClose()
    if ('patient_id' in changes) setConfirming(true)
    else save()
  }

  return (
    <Sheet
      title="Corregir informe"
      onClose={onClose}
      footer={
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onClose}
            className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
          >
            Cancelar
          </button>
          <button
            type="submit"
            form={formId}
            disabled={update.isPending}
            className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
          >
            {update.isPending ? 'Guardando…' : 'Guardar'}
          </button>
        </div>
      }
    >
      <form id={formId} onSubmit={handleSubmit} className="space-y-5">
        <fieldset>
          <legend className="text-sm font-medium text-slate-700">Paciente</legend>
          {report.patient_id === null && (
            <p className="mt-1 text-sm text-amber-900">
              No se pudo identificar a quién pertenece. Revisa el PDF y elige el paciente.
            </p>
          )}
          <div className="mt-2 grid grid-cols-2 gap-2">
            {patients.map((p) => (
              <label
                key={p.id}
                className={`flex min-h-12 cursor-pointer items-center justify-center gap-2 rounded-2xl px-3 font-medium ring-1 ring-inset ${
                  patientId === p.id
                    ? 'bg-brand-700 text-white ring-brand-700'
                    : 'bg-white text-slate-700 ring-slate-300'
                }`}
              >
                <input
                  type="radio"
                  name="patient"
                  value={p.id}
                  checked={patientId === p.id}
                  onChange={() => setPatientId(p.id)}
                  className="sr-only"
                />
                {p.display_name}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor={`${formId}-collected`} className="text-sm font-medium text-slate-700">
              Fecha de la muestra
            </label>
            <input
              id={`${formId}-collected`}
              type="date"
              value={collectedOn}
              max={today}
              onChange={(e) => setCollectedOn(e.target.value)}
              className={inputClass}
            />
          </div>
          <div>
            <label htmlFor={`${formId}-reported`} className="text-sm font-medium text-slate-700">
              Fecha del informe
            </label>
            <input
              id={`${formId}-reported`}
              type="date"
              value={reportedOn}
              max={today}
              onChange={(e) => setReportedOn(e.target.value)}
              className={inputClass}
            />
          </div>
        </div>

        <div>
          <label htmlFor={`${formId}-lab`} className="text-sm font-medium text-slate-700">
            Laboratorio
          </label>
          <input
            id={`${formId}-lab`}
            type="text"
            maxLength={200}
            value={labName}
            onChange={(e) => setLabName(e.target.value)}
            className={inputClass}
          />
        </div>

        {update.isError && (
          <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
            {errorMessage(update.error, 'No se pudo guardar el informe.')}
          </p>
        )}
      </form>

      {confirming && (
        <ConfirmDialog
          title="¿Cambiar el paciente?"
          message={
            report.patient_id === null ? (
              <>
                Este informe y todos sus resultados quedarán en el historial de{' '}
                <strong>{nameOf(patientId)}</strong>.
              </>
            ) : (
              <>
                Los resultados pasarán del historial de{' '}
                <strong>{nameOf(report.patient_id)}</strong> al de{' '}
                <strong>{nameOf(patientId)}</strong>.
              </>
            )
          }
          confirmLabel={update.isPending ? 'Guardando…' : 'Sí, cambiar'}
          busy={update.isPending}
          onConfirm={save}
          onCancel={() => setConfirming(false)}
        />
      )}
    </Sheet>
  )
}
