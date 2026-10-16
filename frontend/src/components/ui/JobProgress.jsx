import { JOB_STATUS_LABELS, isJobFinished, useJob } from '../../lib/jobs.js'

/**
 * Progress of a background job: status, generic message and a progress bar.
 * Polls GET /jobs/{id} (via `useJob`) until the job is done or failed. A page
 * that must react to completion (refresh a list, navigate) calls `useJob` with
 * the same id itself: TanStack Query shares the one poll.
 */
export default function JobProgress({ jobId, title = 'Procesando' }) {
  const { data: job, isError } = useJob(jobId)

  if (isError) {
    return (
      <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
        No se pudo consultar el progreso.
      </p>
    )
  }

  const status = job?.status ?? 'queued'
  const progress = job?.progress ?? 0
  const failed = status === 'failed'
  const retrying = status === 'queued' && (job?.attempts ?? 0) > 0
  const label = retrying ? 'Reintentando…' : JOB_STATUS_LABELS[status]

  return (
    <div className="rounded-2xl bg-white p-4 ring-1 ring-slate-200" aria-live="polite">
      <div className="flex items-baseline justify-between gap-3">
        <p className="font-medium">{title}</p>
        <p className={`text-sm ${failed ? 'text-red-700' : 'text-slate-600'}`}>{label}</p>
      </div>
      <div
        role="progressbar"
        aria-label={title}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
        className="mt-3 h-3 overflow-hidden rounded-full bg-slate-100"
      >
        <div
          className={`h-full rounded-full transition-[width] duration-500 ${
            failed ? 'bg-red-500' : 'bg-brand-600'
          }`}
          style={{ width: `${progress}%` }}
        />
      </div>
      {job?.message && !isJobFinished(job) && (
        <p className="mt-2 text-sm text-slate-600">{job.message}</p>
      )}
    </div>
  )
}
