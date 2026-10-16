import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { usePatient } from '../../contexts/PatientContext.js'
import { panamaToday } from '../../lib/appointments.js'
import { errorMessage } from '../../lib/bitacora.js'
import {
  RUN_STATUS_LABELS,
  STOP_REASON_LABELS,
  estimateRun,
  formatIsoDay,
  formatUsd,
  isRunActive,
  useAnalysisRuns,
  useRejectPending,
  useResumeRun,
  useStartRun,
  useStopRun,
} from '../../lib/analysisRuns.js'
import { formatDateTime } from '../../lib/whatsappImports.js'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'

const NO_RUNS = []
const inputClass =
  'mt-1 min-h-12 w-full rounded-xl border border-slate-300 bg-white px-3 text-base'

const STATUS_STYLES = {
  done: 'bg-emerald-50 text-emerald-800',
  failed: 'bg-red-50 text-red-800',
  stopped: 'bg-slate-100 text-slate-700',
}

function Estimate({ estimate }) {
  const rows = [
    ['Días con mensajes', estimate.days_with_messages],
    ['Ya analizados antes', estimate.days_already_done],
    ['Días a analizar', estimate.days_to_analyze],
    ['Días con algo que leer', estimate.days_with_content],
    ['Mensajes', estimate.messages],
    ['Llamadas al modelo', estimate.calls],
  ]
  return (
    <div className="rounded-xl bg-slate-50 p-3 text-sm" aria-live="polite">
      <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-slate-600">{label}</dt>
            <dd className="text-right font-medium">{value}</dd>
          </div>
        ))}
        <dt className="text-slate-600">Costo aproximado</dt>
        <dd className="text-right font-semibold">{formatUsd(estimate.cost_usd)}</dd>
      </dl>
      {estimate.media_in_progress > 0 && (
        <p className="mt-2 text-amber-800">
          {estimate.media_in_progress} archivo(s) todavía se están transcribiendo o describiendo.
          El análisis los esperará hasta unas 2 horas; conviene iniciarlo cuando terminen.
        </p>
      )}
      {estimate.media_unresolved > estimate.media_in_progress && (
        <p className="mt-2 text-slate-600">
          {estimate.media_unresolved - estimate.media_in_progress} archivo(s) no tienen
          transcripción ni descripción; el análisis no podrá leerlos.
        </p>
      )}
    </div>
  )
}

function StartForm() {
  const { patients } = usePatient()
  const today = panamaToday()
  const [form, setForm] = useState({
    patientId: '',
    dateFrom: `${today.slice(0, 4)}-01-01`,
    dateTo: today,
    crossCheck: false,
    reanalyze: false,
    maxCost: '',
  })
  const [estimate, setEstimate] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(false)
  const start = useStartRun()
  const ids = {
    patient: useId(),
    from: useId(),
    to: useId(),
    cost: useId(),
  }

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
    setEstimate(null)
    setError('')
  }

  function body() {
    return {
      patient_id: Number(form.patientId),
      date_from: form.dateFrom,
      date_to: form.dateTo,
      cross_check: form.crossCheck,
      reanalyze: form.reanalyze,
    }
  }

  async function calculate(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const result = await estimateRun(body())
      setForm((f) => (f.maxCost ? f : { ...f, maxCost: String(result.max_cost_usd) }))
      setEstimate(result)
    } catch (err) {
      setError(errorMessage(err, 'No se pudo calcular.'))
    } finally {
      setBusy(false)
    }
  }

  async function confirmStart() {
    setError('')
    try {
      await start.mutateAsync({
        ...body(),
        max_cost_usd: form.maxCost ? Number(form.maxCost) : null,
      })
      setConfirming(false)
      setEstimate(null)
    } catch (err) {
      setConfirming(false)
      setError(errorMessage(err, 'No se pudo iniciar el análisis.'))
    }
  }

  const patient = patients.find((p) => String(p.id) === form.patientId)
  const ready = form.patientId && form.dateFrom && form.dateTo

  return (
    <form onSubmit={calculate} className="space-y-4 rounded-2xl bg-white p-4 ring-1 ring-slate-200">
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
            Desde
          </label>
          <input
            id={ids.from}
            type="date"
            value={form.dateFrom}
            max={today}
            onChange={(e) => update('dateFrom', e.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor={ids.to} className="font-medium">
            Hasta
          </label>
          <input
            id={ids.to}
            type="date"
            value={form.dateTo}
            max={today}
            onChange={(e) => update('dateTo', e.target.value)}
            className={inputClass}
          />
        </div>
      </div>
      <label className="flex min-h-12 cursor-pointer gap-3 rounded-xl border border-slate-200 p-3">
        <input
          type="checkbox"
          checked={form.crossCheck}
          onChange={(e) => update('crossCheck', e.target.checked)}
          className="mt-1 size-5 accent-brand-700"
        />
        <span>
          <span className="block font-medium">Solo lo que falta en la bitácora</span>
          <span className="block text-sm text-slate-600">
            Para un paciente cuya bitácora ya está completa: propone solo eventos que no están
            registrados.
          </span>
        </span>
      </label>
      <label className="flex min-h-12 cursor-pointer gap-3 rounded-xl border border-slate-200 p-3">
        <input
          type="checkbox"
          checked={form.reanalyze}
          onChange={(e) => update('reanalyze', e.target.checked)}
          className="mt-1 size-5 accent-brand-700"
        />
        <span>
          <span className="block font-medium">Volver a analizar días ya analizados</span>
          <span className="block text-sm text-slate-600">
            Normalmente se omiten los días que otro análisis igual ya leyó.
          </span>
        </span>
      </label>

      {error && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={!ready || busy}
        className="min-h-12 w-full rounded-2xl border border-brand-700 px-5 font-semibold text-brand-800 active:bg-brand-50 disabled:opacity-50"
      >
        {busy ? 'Calculando…' : 'Calcular costo'}
      </button>

      {estimate && (
        <>
          <Estimate estimate={estimate} />
          <div>
            <label htmlFor={ids.cost} className="font-medium">
              Límite de costo (USD)
            </label>
            <input
              id={ids.cost}
              type="number"
              inputMode="decimal"
              min="0.01"
              step="0.01"
              value={form.maxCost}
              onChange={(e) => setForm((f) => ({ ...f, maxCost: e.target.value }))}
              className={inputClass}
            />
            <p className="mt-1 text-xs text-slate-500">
              El análisis se detiene al llegar a este costo; luego se puede reanudar.
            </p>
          </div>
          <button
            type="button"
            disabled={estimate.days_to_analyze === 0}
            onClick={() => setConfirming(true)}
            className="min-h-12 w-full rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800 disabled:opacity-50"
          >
            Iniciar análisis
          </button>
        </>
      )}

      {confirming && (
        <ConfirmDialog
          title="¿Iniciar el análisis histórico?"
          message={`Se analizarán ${estimate.days_to_analyze} día(s) de mensajes de ${
            patient?.display_name ?? 'este paciente'
          } (costo aproximado ${formatUsd(estimate.cost_usd)}). Las sugerencias aparecerán en Pendientes.`}
          confirmLabel="Iniciar"
          busy={start.isPending}
          onConfirm={confirmStart}
          onCancel={() => setConfirming(false)}
        />
      )}
    </form>
  )
}

function RunCard({ run, patientName, onAction }) {
  const stats = run.stats ?? {}
  const done = stats.days_done?.length ?? 0
  const total = stats.days_total ?? null
  const percent = total ? Math.round((100 * done) / total) : 0
  return (
    <li className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-slate-900">
          {patientName} · {formatIsoDay(run.date_from)} – {formatIsoDay(run.date_to)}
        </p>
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
            STATUS_STYLES[run.status] ?? 'bg-amber-50 text-amber-800'
          }`}
        >
          {RUN_STATUS_LABELS[run.status] ?? run.status}
        </span>
      </div>
      <p className="mt-1 text-sm text-slate-600">
        {[
          run.cross_check ? 'Solo lo que falta' : 'Reconstruir historia',
          run.created_by_name,
          formatDateTime(run.created_at),
        ]
          .filter(Boolean)
          .join(' · ')}
      </p>
      {total !== null && (
        <div className="mt-3">
          <div className="flex justify-between text-sm text-slate-600">
            <span>
              {done} de {total} días
            </span>
            <span>
              {formatUsd(stats.cost_usd)} de {formatUsd(run.max_cost_usd)}
            </span>
          </div>
          <div
            role="progressbar"
            aria-label="Días analizados"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            className="mt-1 h-2 overflow-hidden rounded-full bg-slate-100"
          >
            <div className="h-full rounded-full bg-brand-600" style={{ width: `${percent}%` }} />
          </div>
        </div>
      )}
      <p className="mt-2 text-sm text-slate-800">
        {stats.suggestions_created ?? 0} sugerencia(s) creadas
        {run.pending_suggestions > 0 && (
          <>
            {' · '}
            <Link to="/pendientes" className="font-medium text-brand-700 underline">
              {run.pending_suggestions} pendiente(s)
            </Link>
          </>
        )}
      </p>
      {run.status === 'stopped' && run.stop_reason && (
        <p className="mt-1 text-sm text-slate-600">{STOP_REASON_LABELS[run.stop_reason]}</p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {isRunActive(run) && (
          <button
            type="button"
            onClick={() => onAction('stop', run)}
            className="min-h-11 rounded-xl border border-slate-300 px-4 text-sm font-semibold text-slate-700 active:bg-slate-50"
          >
            Detener
          </button>
        )}
        {(run.status === 'stopped' || run.status === 'failed') && (
          <button
            type="button"
            onClick={() => onAction('resume', run)}
            className="min-h-11 rounded-xl border border-brand-700 px-4 text-sm font-semibold text-brand-800 active:bg-brand-50"
          >
            Reanudar
          </button>
        )}
        {!isRunActive(run) && run.pending_suggestions > 0 && (
          <button
            type="button"
            onClick={() => onAction('reject', run)}
            className="min-h-11 rounded-xl px-2 text-sm font-medium text-red-700 active:bg-red-50"
          >
            Rechazar sus pendientes
          </button>
        )}
      </div>
    </li>
  )
}

/** «Análisis histórico» (admins): start a backfill analysis and follow past runs. */
export default function BackfillPanel() {
  const { patients } = usePatient()
  const { data: runs = NO_RUNS, isError } = useAnalysisRuns()
  const stop = useStopRun()
  const resume = useResumeRun()
  const reject = useRejectPending()
  const [action, setAction] = useState(null) // { kind, run }
  const [maxCost, setMaxCost] = useState('')
  const [error, setError] = useState('')
  const costId = useId()

  const names = Object.fromEntries(patients.map((p) => [p.id, p.display_name]))

  function open(kind, run) {
    setError('')
    setMaxCost(kind === 'resume' ? String(run.max_cost_usd) : '')
    setAction({ kind, run })
  }

  async function confirm() {
    const { kind, run } = action
    setError('')
    try {
      if (kind === 'stop') await stop.mutateAsync(run.id)
      if (kind === 'resume')
        await resume.mutateAsync({ id: run.id, maxCostUsd: maxCost ? Number(maxCost) : null })
      if (kind === 'reject') await reject.mutateAsync(run.id)
      setAction(null)
    } catch (err) {
      setError(errorMessage(err, 'No se pudo completar la acción.'))
    }
  }

  const dialogs = {
    stop: {
      title: '¿Detener el análisis?',
      message: 'Las sugerencias ya creadas se quedan en Pendientes. Puedes reanudarlo después.',
      confirmLabel: 'Detener',
      danger: true,
    },
    resume: {
      title: '¿Reanudar el análisis?',
      message: 'Continúa con los días que faltan.',
      confirmLabel: 'Reanudar',
    },
    reject: {
      title: '¿Rechazar sus sugerencias pendientes?',
      message: `Se rechazarán ${action?.run.pending_suggestions ?? 0} sugerencia(s) pendiente(s) de este análisis. Las ya aprobadas no cambian.`,
      confirmLabel: 'Rechazar',
      danger: true,
    },
  }
  const dialog = action ? dialogs[action.kind] : null
  const busy = stop.isPending || resume.isPending || reject.isPending

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">Análisis histórico</h2>
        <p className="mt-1 text-sm text-slate-600">
          Lee los mensajes ya importados de un periodo y propone entradas de bitácora, citas y
          médicos para un paciente. Todo queda en Pendientes para revisar. Tiene costo: calcúlalo
          antes de iniciar.
        </p>
      </div>
      <StartForm />
      {isError && <p className="text-sm text-red-800">No se pudo cargar la lista de análisis.</p>}
      {runs.length > 0 && (
        <ul className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {runs.map((run) => (
            <RunCard
              key={run.id}
              run={run}
              patientName={names[run.patient_id] ?? 'Paciente'}
              onAction={open}
            />
          ))}
        </ul>
      )}
      {dialog && (
        <ConfirmDialog
          title={dialog.title}
          message={
            action.kind === 'resume' ? (
              <span className="block">
                {dialog.message}
                <label htmlFor={costId} className="mt-3 block font-medium text-slate-900">
                  Límite de costo (USD)
                </label>
                <input
                  id={costId}
                  type="number"
                  inputMode="decimal"
                  min="0.01"
                  step="0.01"
                  value={maxCost}
                  onChange={(e) => setMaxCost(e.target.value)}
                  className={inputClass}
                />
              </span>
            ) : (
              dialog.message
            )
          }
          confirmLabel={dialog.confirmLabel}
          danger={dialog.danger}
          busy={busy}
          error={error}
          onConfirm={confirm}
          onCancel={() => setAction(null)}
        />
      )}
    </div>
  )
}
