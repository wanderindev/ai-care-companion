import { useEffect, useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { SparklesIcon } from '@heroicons/react/24/outline'
import { usePatient } from '../../contexts/PatientContext.js'
import { errorMessage } from '../../lib/bitacora.js'
import {
  estimateDoctorInference,
  formatInferenceCost,
  useStartDoctorInference,
} from '../../lib/doctorInference.js'
import { isJobFinished, useJob } from '../../lib/jobs.js'
import { SUGGESTIONS_KEY } from '../../lib/suggestions.js'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import JobProgress from '../ui/JobProgress.jsx'

const inputClass =
  'mt-1 min-h-12 w-full rounded-xl border border-slate-300 bg-white px-3 text-base'

function plural(n, one, many) {
  return n === 1 ? `1 ${one}` : `${n} ${many}`
}

/** The running job; once it ends, refreshes «Pendientes» and reports the result. */
function RunningInference({ jobId, onFinished }) {
  const queryClient = useQueryClient()
  const { data: job } = useJob(jobId)
  const finished = isJobFinished(job)
  useEffect(() => {
    if (!finished) return
    queryClient.invalidateQueries({ queryKey: SUGGESTIONS_KEY })
    onFinished(job)
    // Runs once per finished job; `job` / `onFinished` change on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished, jobId, queryClient])
  return <JobProgress jobId={jobId} title="Buscando médicos con IA" />
}

/**
 * «Completar médicos con IA» (admins, #125): pick a patient (and optionally a
 * date range), see how many entries without a doctor would be sent and the
 * estimated cost, confirm, follow the job, then go to «Pendientes».
 */
export default function DoctorInferencePanel() {
  const { patients } = usePatient()
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ patientId: '', dateFrom: '', dateTo: '' })
  const [estimate, setEstimate] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [jobId, setJobId] = useState(null)
  const [result, setResult] = useState(null) // { failed, message }
  const start = useStartDoctorInference()
  const ids = { patient: useId(), from: useId(), to: useId() }

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
    setEstimate(null)
    setError('')
  }

  function body() {
    return {
      patient_id: Number(form.patientId),
      date_from: form.dateFrom || null,
      date_to: form.dateTo || null,
    }
  }

  async function calculate(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const est = await estimateDoctorInference(body())
      if (est.running_job_id) setJobId(est.running_job_id)
      else setEstimate(est)
    } catch (err) {
      setError(errorMessage(err, 'No se pudo calcular.'))
    } finally {
      setBusy(false)
    }
  }

  async function confirmStart() {
    try {
      const res = await start.mutateAsync(body())
      setConfirming(false)
      setEstimate(null)
      setJobId(res.job_id)
    } catch (err) {
      setConfirming(false)
      setError(errorMessage(err, 'No se pudo iniciar.'))
    }
  }

  const patient = patients.find((p) => String(p.id) === form.patientId)

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl px-4 font-semibold text-brand-700 ring-1 ring-brand-600 active:bg-brand-50 sm:w-auto"
      >
        <SparklesIcon className="size-5" aria-hidden="true" />
        Completar médicos con IA
      </button>
    )
  }

  return (
    <div className="space-y-3 rounded-2xl bg-white p-4 ring-1 ring-slate-200">
      <div>
        <h2 className="flex items-center gap-2 text-lg font-semibold">
          <SparklesIcon className="size-5 text-brand-700" aria-hidden="true" />
          Completar médicos con IA
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          La IA busca el médico de las entradas de la bitácora que no lo tienen (consultas,
          exámenes, procedimientos, terapias…), según el texto, las entradas y citas cercanas y la
          especialidad de cada médico. Solo propone los casos claros, y cada propuesta queda en
          Pendientes para aprobarla o rechazarla. Tiene costo: calcúlalo antes de iniciar.
        </p>
      </div>

      {jobId ? (
        <RunningInference
          jobId={jobId}
          onFinished={(job) => {
            setJobId(null)
            setResult({ failed: job.status === 'failed', message: job.message })
          }}
        />
      ) : (
        <form onSubmit={calculate} className="space-y-3">
          <div>
            <label htmlFor={ids.patient} className="font-medium">
              Paciente
            </label>
            <select
              id={ids.patient}
              value={form.patientId}
              onChange={(e) => update('patientId', e.target.value)}
              className={inputClass}
            >
              <option value="">Elige un paciente</option>
              {patients.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name}
                </option>
              ))}
            </select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor={ids.from} className="font-medium">
                Desde <span className="font-normal text-slate-500">(opcional)</span>
              </label>
              <input
                id={ids.from}
                type="date"
                value={form.dateFrom}
                onChange={(e) => update('dateFrom', e.target.value)}
                className={inputClass}
              />
            </div>
            <div>
              <label htmlFor={ids.to} className="font-medium">
                Hasta <span className="font-normal text-slate-500">(opcional)</span>
              </label>
              <input
                id={ids.to}
                type="date"
                value={form.dateTo}
                onChange={(e) => update('dateTo', e.target.value)}
                className={inputClass}
              />
            </div>
          </div>

          {error && (
            <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={!form.patientId || busy}
            className="min-h-12 w-full rounded-2xl border border-brand-700 px-5 font-semibold text-brand-800 active:bg-brand-50 disabled:opacity-50"
          >
            {busy ? 'Calculando…' : 'Calcular costo'}
          </button>

          {estimate && (
            <div className="rounded-xl bg-slate-50 p-3 text-sm" aria-live="polite">
              {estimate.candidates === 0 ? (
                <p className="text-slate-700">
                  No hay entradas sin médico por revisar para este paciente en ese periodo.
                </p>
              ) : (
                <>
                  <p className="text-slate-800">
                    {plural(estimate.candidates, 'entrada sin médico', 'entradas sin médico')} en{' '}
                    {plural(estimate.batches, 'consulta', 'consultas')} a la IA. Costo aproximado:{' '}
                    <span className="font-semibold">{formatInferenceCost(estimate.cost_usd)}</span>
                    .
                  </p>
                  <button
                    type="button"
                    onClick={() => setConfirming(true)}
                    className="mt-3 min-h-12 w-full rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
                  >
                    Iniciar
                  </button>
                </>
              )}
            </div>
          )}
        </form>
      )}

      {result && (
        <div
          role="status"
          className={`rounded-xl px-3 py-2 text-sm ${
            result.failed ? 'bg-red-50 text-red-800' : 'bg-emerald-50 text-emerald-900'
          }`}
        >
          <p>
            {result.failed
              ? `La búsqueda no terminó${result.message ? `: ${result.message}` : '.'}`
              : result.message || 'Búsqueda terminada.'}
          </p>
          {!result.failed && (
            <Link
              to="/pendientes"
              className="mt-1 flex min-h-11 items-center font-semibold text-brand-700 underline"
            >
              Revisar en Pendientes
            </Link>
          )}
        </div>
      )}

      {!jobId && (
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="min-h-11 px-2 text-sm font-medium text-slate-600"
        >
          Cerrar
        </button>
      )}

      {confirming && (
        <ConfirmDialog
          title="¿Completar médicos con IA?"
          message={`Se revisarán ${plural(
            estimate.candidates,
            'entrada',
            'entradas',
          )} de ${patient?.display_name ?? 'este paciente'} (costo aproximado ${formatInferenceCost(
            estimate.cost_usd,
          )}). Las propuestas aparecerán en Pendientes; nada cambia hasta que alguien las apruebe.`}
          confirmLabel="Iniciar"
          busy={start.isPending}
          onConfirm={confirmStart}
          onCancel={() => setConfirming(false)}
        />
      )}
    </div>
  )
}
