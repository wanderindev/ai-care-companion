import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { SparklesIcon } from '@heroicons/react/24/outline'
import { errorMessage } from '../../lib/bitacora.js'
import { isJobFinished, useJob } from '../../lib/jobs.js'
import {
  TRIAGE_CONFIDENCE_LABELS,
  TRIAGE_REASON_LABELS,
  useAcceptAllTriage,
  useAcceptTriage,
  useRejectTriage,
  useStartTriage,
  useTriageEstimate,
} from '../../lib/analytes.js'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import JobProgress from '../ui/JobProgress.jsx'

function plural(n, one, many) {
  return n === 1 ? `1 ${one}` : `${n} ${many}`
}

function formatCost(cost) {
  if (cost == null) return 'desconocido'
  return cost < 0.01 ? 'menos de US$0.01' : `unos US$${cost.toFixed(2)}`
}

/**
 * Once the triage job ends: refreshes the review list and the estimate (both
 * under `['analytes']`) and calls `onFinished(job)` so the panel drops the
 * finished bar and offers «Revisar con IA» again.
 */
function RunningTriage({ jobId, onFinished }) {
  const queryClient = useQueryClient()
  const { data: job } = useJob(jobId)
  const finished = isJobFinished(job)
  useEffect(() => {
    if (!finished) return
    queryClient.invalidateQueries({ queryKey: ['analytes'] })
    onFinished(job)
    // Runs once per finished job; `job` / `onFinished` change on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished, jobId, queryClient])
  return <JobProgress jobId={jobId} title="Revisión con IA" />
}

/**
 * «Revisar con IA» (#100): queue the triage job (after showing its estimated
 * cost) and «Aceptar todas las propuestas». The job only proposes.
 */
export function TriagePanel({ onNotice }) {
  const estimate = useTriageEstimate()
  const start = useStartTriage()
  const acceptAll = useAcceptAllTriage()
  const [jobId, setJobId] = useState(null)
  const [asking, setAsking] = useState(null) // 'start' | 'accept-all'

  const data = estimate.data
  if (!data) return null
  const runningId = jobId ?? data.running_job_id

  return (
    <div className="mt-4 space-y-3">
      {runningId ? (
        <RunningTriage
          jobId={runningId}
          onFinished={(job) => {
            setJobId(null)
            onNotice(
              job.status === 'failed'
                ? `La revisión con IA no terminó${job.message ? `: ${job.message}` : '.'}`
                : job.message || 'Revisión con IA terminada.',
            )
          }}
        />
      ) : (
        data.analytes > 0 && (
          <div className="rounded-2xl bg-white p-4 ring-1 ring-slate-200">
            <p className="text-sm text-slate-700">
              {plural(data.analytes, 'examen aún no tiene', 'exámenes aún no tienen')} una
              propuesta de la IA.
            </p>
            <button
              type="button"
              onClick={() => setAsking('start')}
              className="mt-3 flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800 sm:w-auto"
            >
              <SparklesIcon className="size-5" aria-hidden="true" />
              Revisar con IA
            </button>
          </div>
        )
      )}
      {data.pending_proposals > 0 && (
        <button
          type="button"
          onClick={() => setAsking('accept-all')}
          className="flex min-h-12 w-full items-center justify-center rounded-2xl px-4 font-semibold text-brand-700 ring-1 ring-brand-600 active:bg-brand-50 sm:w-auto"
        >
          Aceptar todas las propuestas ({data.pending_proposals})
        </button>
      )}

      {asking === 'start' && (
        <ConfirmDialog
          title="Revisar con IA"
          message={
            <>
              Claude revisará {plural(data.analytes, 'examen', 'exámenes')} en{' '}
              {plural(data.batches, 'consulta', 'consultas')} (costo estimado:{' '}
              {formatCost(data.cost_usd)}). Solo propone: nada cambia hasta que aceptes cada
              propuesta.
            </>
          }
          confirmLabel="Empezar"
          busy={start.isPending}
          error={start.isError ? errorMessage(start.error, 'No se pudo empezar.') : ''}
          onCancel={() => {
            start.reset()
            setAsking(null)
          }}
          onConfirm={() =>
            start.mutate(undefined, {
              onSuccess: (res) => {
                setJobId(res.job_id)
                setAsking(null)
              },
            })
          }
        />
      )}
      {asking === 'accept-all' && (
        <ConfirmDialog
          title="Aceptar todas las propuestas"
          message={`Se aplicarán ${plural(data.pending_proposals, 'propuesta', 'propuestas')}: cada examen se combinará o se confirmará como indica su tarjeta. Las que no se puedan aplicar quedarán pendientes.`}
          confirmLabel="Aceptar todas"
          busy={acceptAll.isPending}
          error={acceptAll.isError ? errorMessage(acceptAll.error, 'No se pudo aplicar.') : ''}
          onCancel={() => {
            acceptAll.reset()
            setAsking(null)
          }}
          onConfirm={() =>
            acceptAll.mutate(undefined, {
              onSuccess: (res) => {
                setAsking(null)
                onNotice(
                  `${plural(res.accepted, 'propuesta aplicada', 'propuestas aplicadas')}.` +
                    (res.failed
                      ? ` ${plural(res.failed, 'no se pudo aplicar', 'no se pudieron aplicar')}; revísalas a mano.`
                      : ''),
                )
              },
            })
          }
        />
      )}
    </div>
  )
}

function proposalText(analyte, triage) {
  if (triage.action === 'merge_into') {
    return triage.target_name
      ? `Combinar con «${triage.target_name}».`
      : 'Combinar con un examen que ya no existe.'
  }
  if (triage.action === 'keep_as_new') {
    return triage.display_name && triage.display_name !== analyte.canonical_name
      ? `Confirmar como examen nuevo, con el nombre «${triage.display_name}».`
      : 'Confirmar como examen nuevo.'
  }
  return 'La IA no está segura; revísalo a mano.'
}

/** The AI's pending proposal inside an analyte card: «Aceptar» / «Descartar». */
export function TriageProposal({ analyte, onNotice }) {
  const accept = useAcceptTriage()
  const reject = useRejectTriage()
  const triage = analyte.triage
  if (!triage || triage.status !== 'pending') return null
  const busy = accept.isPending || reject.isPending
  const canAccept = triage.action !== 'unsure' && !(triage.action === 'merge_into' && !triage.target_name)
  const details = [
    TRIAGE_REASON_LABELS[triage.reason_code],
    `confianza ${TRIAGE_CONFIDENCE_LABELS[triage.confidence]}`,
    triage.units_match === false ? 'las unidades no coinciden' : null,
    triage.action === 'merge_into' && triage.units_match === null
      ? 'sin unidades para comparar'
      : null,
  ].filter(Boolean)
  const error = accept.error ?? reject.error

  return (
    <div className="mt-3 rounded-xl bg-brand-50 p-3">
      <p className="flex items-center gap-1.5 text-xs font-semibold text-brand-800 uppercase">
        <SparklesIcon className="size-4" aria-hidden="true" />
        Propuesta de la IA
      </p>
      <p className="mt-1 font-medium text-slate-800">{proposalText(analyte, triage)}</p>
      <p className="mt-0.5 text-sm text-slate-600">{details.join(' · ')}</p>
      {triage.error && (
        <p className="mt-1 text-sm text-amber-800">
          No se pudo aplicar automáticamente; revísala a mano.
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(error, 'No se pudo guardar.')}
        </p>
      )}
      <div className="mt-3 grid grid-cols-2 gap-2">
        {canAccept && (
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              accept.mutate(analyte.id, {
                onSuccess: (saved) =>
                  onNotice(
                    triage.action === 'merge_into'
                      ? `«${analyte.canonical_name}» combinado con «${saved.canonical_name}».`
                      : `«${saved.canonical_name}» confirmado.`,
                  ),
              })
            }
            className="min-h-12 rounded-2xl bg-brand-700 px-3 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
          >
            Aceptar
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => reject.mutate(analyte.id)}
          className={`min-h-12 rounded-2xl bg-white px-3 font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60 ${canAccept ? '' : 'col-span-2'}`}
        >
          Descartar
        </button>
      </div>
    </div>
  )
}
