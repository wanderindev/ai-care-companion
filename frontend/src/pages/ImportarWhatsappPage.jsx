import { useEffect, useId, useRef, useState } from 'react'
import { ArrowUpTrayIcon, ChatBubbleLeftRightIcon } from '@heroicons/react/24/outline'
import { useMe } from '../lib/auth.js'
import { errorMessage, formatSize } from '../lib/bitacora.js'
import { isJobFinished, useJob } from '../lib/jobs.js'
import {
  MEDIA_BUCKET_LABELS,
  MODE_OPTIONS,
  STATUS_LABELS,
  cancelWhatsappImport,
  describeCounts,
  formatDateTime,
  formatDay,
  modeLabel,
  sumCounts,
  uploadWhatsappExport,
  useInvalidateImports,
  useWhatsappImports,
} from '../lib/whatsappImports.js'
import JobProgress from '../components/ui/JobProgress.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'
import BackfillPanel from '../components/imports/BackfillPanel.jsx'
import ExtensionPanel from '../components/imports/ExtensionPanel.jsx'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'

const NO_IMPORTS = []

const STATUS_STYLES = {
  done: 'bg-emerald-50 text-emerald-800',
  failed: 'bg-red-50 text-red-800',
  cancelled: 'bg-slate-100 text-slate-600',
}

function StatusBadge({ status }) {
  return (
    <span
      className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
        STATUS_STYLES[status] ?? 'bg-amber-50 text-amber-800'
      }`}
    >
      {STATUS_LABELS[status] ?? status}
    </span>
  )
}

function ImportStats({ stats }) {
  const messages = stats?.messages
  const media = stats?.media ?? {}
  if (!messages) return null
  return (
    <dl className="mt-3 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
      <dt className="text-slate-500">Mensajes nuevos</dt>
      <dd className="font-medium">
        {messages.new} de {messages.total}
        {messages.duplicate > 0 && (
          <span className="font-normal text-slate-500"> ({messages.duplicate} ya estaban)</span>
        )}
      </dd>
      {Object.entries(MEDIA_BUCKET_LABELS).map(([bucket, label]) =>
        sumCounts(media[bucket]) > 0 ? (
          <div key={bucket} className="contents">
            <dt className="text-slate-500">{label}</dt>
            <dd className="font-medium">{describeCounts(media[bucket])}</dd>
          </div>
        ) : null,
      )}
      {stats.unparsed_lines > 0 && (
        <>
          <dt className="text-slate-500">Líneas no reconocidas</dt>
          <dd className="font-medium">{stats.unparsed_lines}</dd>
        </>
      )}
    </dl>
  )
}

function ImportCard({ item, onDiscard }) {
  const files =
    sumCounts(item.stats?.media?.uploaded) + sumCounts(item.stats?.media?.added_to_existing)
  return (
    <li className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-slate-900">{formatDateTime(item.created_at)}</p>
        <StatusBadge status={item.status} />
      </div>
      <p className="mt-1 text-sm text-slate-600">
        {[modeLabel(item.mode), item.uploaded_by_name, formatSize(item.size)]
          .filter(Boolean)
          .join(' · ')}
      </p>
      {item.range_from && (
        <p className="text-sm text-slate-600">
          Mensajes del {formatDay(item.range_from)} al {formatDay(item.range_to)}
        </p>
      )}
      {item.status === 'done' && item.stats?.messages && (
        <p className="mt-1 text-sm font-medium text-slate-800">
          {item.stats.messages.new} mensajes nuevos · {files} archivos guardados
        </p>
      )}
      {item.status === 'done' && item.awaiting_media && (
        <p className="mt-1 text-sm text-amber-800">
          {item.media_completed_at
            ? 'Procesando los archivos de la extensión'
            : 'Esperando los archivos de la extensión'}
        </p>
      )}
      {item.status === 'done' && (
        <details className="mt-1">
          <summary className="min-h-11 cursor-pointer py-2 text-sm font-medium text-brand-700">
            Ver detalle
          </summary>
          <ImportStats stats={item.stats} />
        </details>
      )}
      {item.status === 'uploading' && onDiscard && (
        <button
          type="button"
          onClick={() => onDiscard(item)}
          className="mt-2 min-h-11 rounded-xl px-1 text-sm font-medium text-red-700 active:bg-red-50"
        >
          Descartar subida incompleta
        </button>
      )}
    </li>
  )
}

function ImportForm({ onStarted, disabled }) {
  const [file, setFile] = useState(null)
  const [mode, setMode] = useState('labs_only')
  const fileId = useId()

  function submit(event) {
    event.preventDefault()
    if (!file) return
    onStarted({ file, mode, patientFocusId: null })
  }

  return (
    <form onSubmit={submit} className="space-y-5 rounded-2xl bg-white p-4 ring-1 ring-slate-200">
      <div>
        <label htmlFor={fileId} className="font-medium">
          Archivo .zip exportado del grupo
        </label>
        <input
          id={fileId}
          type="file"
          accept=".zip,application/zip"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="mt-2 block w-full text-base file:mr-3 file:min-h-12 file:rounded-xl file:border-0 file:bg-brand-50 file:px-4 file:font-semibold file:text-brand-800"
        />
        {file && <p className="mt-1 text-sm text-slate-500">{formatSize(file.size)}</p>}
      </div>

      <fieldset>
        <legend className="font-medium">Modo</legend>
        <div className="mt-2 space-y-2">
          {MODE_OPTIONS.map((option) => (
            <label
              key={option.value}
              className={`flex min-h-12 cursor-pointer gap-3 rounded-xl border p-3 ${
                mode === option.value ? 'border-brand-600 bg-brand-50' : 'border-slate-200'
              }`}
            >
              <input
                type="radio"
                name="mode"
                value={option.value}
                checked={mode === option.value}
                onChange={() => setMode(option.value)}
                className="mt-1 size-5 accent-brand-700"
              />
              <span>
                <span className="block font-medium">{option.label}</span>
                <span className="block text-sm text-slate-600">{option.help}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {mode === 'backfill' && (
        <p className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-600">
          Los audios se transcriben y las fotos y PDF se describen automáticamente. El análisis
          del periodo para un paciente se inicia después, en «Análisis histórico».
        </p>
      )}

      <button
        type="submit"
        disabled={!file || disabled}
        className="flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800 disabled:opacity-50"
      >
        <ArrowUpTrayIcon className="size-5" aria-hidden="true" />
        Subir e importar
      </button>
    </form>
  )
}

/** Ajustes → Importar WhatsApp (admins with `can_import`, #96): upload an export zip, follow its job, past imports. */
export default function ImportarWhatsappPage() {
  const { data: me } = useMe()
  const { data: imports = NO_IMPORTS, isPending, isError, error, refetch } = useWhatsappImports()
  const invalidate = useInvalidateImports()
  // phase: idle | uploading | processing
  const [phase, setPhase] = useState('idle')
  const [fraction, setFraction] = useState(0)
  const [importId, setImportId] = useState(null)
  const [jobId, setJobId] = useState(null)
  const [uploadError, setUploadError] = useState('')
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [discarding, setDiscarding] = useState(null)
  const [discardBusy, setDiscardBusy] = useState(false)
  const [discardError, setDiscardError] = useState('')

  async function discard() {
    setDiscardBusy(true)
    setDiscardError('')
    try {
      await cancelWhatsappImport(discarding.id)
      setDiscarding(null)
      invalidate()
    } catch (err) {
      setDiscardError(errorMessage(err, 'No se pudo descartar.'))
    } finally {
      setDiscardBusy(false)
    }
  }
  const controller = useRef(null)
  const { data: job } = useJob(jobId)
  const jobDone = isJobFinished(job)

  useEffect(() => {
    if (jobDone) invalidate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobDone])

  useEffect(() => () => controller.current?.abort(), [])

  async function start({ file, mode, patientFocusId }) {
    const abort = new AbortController()
    controller.current = abort
    setUploadError('')
    setFraction(0)
    setJobId(null)
    setImportId(null)
    setPhase('uploading')
    try {
      const done = await uploadWhatsappExport(file, {
        mode,
        patientFocusId,
        signal: abort.signal,
        onCreated: (created) => {
          setImportId(created.id)
          invalidate()
        },
        onProgress: setFraction,
      })
      setJobId(done.job_id)
      setPhase('processing')
    } catch (err) {
      if (abort.signal.aborted) return
      setUploadError(errorMessage(err, 'No se pudo subir el archivo.'))
      setPhase('idle')
    } finally {
      invalidate()
    }
  }

  async function cancelUpload() {
    controller.current?.abort()
    setConfirmCancel(false)
    if (importId) {
      try {
        await cancelWhatsappImport(importId)
      } catch {
        // Already completed or gone: nothing to cancel.
      }
    }
    setPhase('idle')
    invalidate()
  }

  if (me && !me.can_import) {
    return (
      <section>
        <h1 className="text-2xl font-semibold">Importar WhatsApp</h1>
        <p className="mt-3 text-slate-600">
          Solo quien administra las importaciones puede importar el chat.
        </p>
      </section>
    )
  }

  const current = imports.find((i) => i.id === importId)
  const percent = Math.round(fraction * 100)

  return (
    <section className="space-y-6 pb-16 lg:pb-0">
      <div>
        <h1 className="text-2xl font-semibold">Importar WhatsApp</h1>
        <p className="mt-1 text-sm text-slate-600">
          Exporta el chat del grupo familiar (con archivos) y súbelo aquí. Los mensajes que ya
          estaban guardados no se duplican.
        </p>
      </div>

      {phase === 'idle' && <ImportForm onStarted={start} disabled={false} />}

      {uploadError && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {uploadError}
        </p>
      )}

      {phase === 'uploading' && (
        <div className="rounded-2xl bg-white p-4 ring-1 ring-slate-200" aria-live="polite">
          <div className="flex items-baseline justify-between gap-3">
            <p className="font-medium">Subiendo el archivo</p>
            <p className="text-sm text-slate-600">{percent} %</p>
          </div>
          <div
            role="progressbar"
            aria-label="Subiendo el archivo"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            className="mt-3 h-3 overflow-hidden rounded-full bg-slate-100"
          >
            <div
              className="h-full rounded-full bg-brand-600 transition-[width] duration-300"
              style={{ width: `${percent}%` }}
            />
          </div>
          <p className="mt-2 text-sm text-slate-600">No cierres esta página hasta que termine.</p>
          <button
            type="button"
            onClick={() => setConfirmCancel(true)}
            className="mt-3 min-h-12 w-full rounded-2xl border border-slate-300 px-5 font-semibold text-slate-700 active:bg-slate-50"
          >
            Cancelar subida
          </button>
        </div>
      )}

      {phase === 'processing' && jobId && (
        <div className="space-y-3">
          <JobProgress jobId={jobId} title="Importando mensajes" />
          {jobDone && job?.status === 'done' && current?.stats?.messages && (
            <div className="rounded-2xl bg-white p-4 ring-1 ring-slate-200">
              <p className="font-medium">Resultado</p>
              <ImportStats stats={current.stats} />
            </div>
          )}
          {jobDone && job?.status === 'failed' && (
            <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
              {job.error?.split(': ').slice(1).join(': ') || 'No se pudo importar el archivo.'}
            </p>
          )}
          {jobDone && (
            <button
              type="button"
              onClick={() => {
                setPhase('idle')
                setJobId(null)
              }}
              className="min-h-12 w-full rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
            >
              Importar otro archivo
            </button>
          )}
        </div>
      )}

      <div>
        <h2 className="text-lg font-semibold">Importaciones anteriores</h2>
        <div className="mt-3">
          {isPending ? (
            <CardSkeletons count={3} />
          ) : isError ? (
            <ErrorState
              message={errorMessage(error, 'No se pudo cargar la lista.')}
              onRetry={() => refetch()}
            />
          ) : imports.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
              <ChatBubbleLeftRightIcon
                className="mx-auto size-10 text-slate-400"
                aria-hidden="true"
              />
              <p className="mt-3 font-medium text-slate-800">Todavía no hay importaciones.</p>
            </div>
          ) : (
            <ul className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {imports.map((item) => (
                <ImportCard
                  key={item.id}
                  item={item}
                  onDiscard={phase === 'uploading' && item.id === importId ? null : setDiscarding}
                />
              ))}
            </ul>
          )}
        </div>
      </div>

      <BackfillPanel />

      <ExtensionPanel />

      {discarding && (
        <ConfirmDialog
          title="¿Descartar la subida incompleta?"
          message="Se borrarán las partes ya subidas de ese archivo."
          confirmLabel="Descartar"
          danger
          busy={discardBusy}
          error={discardError}
          onConfirm={discard}
          onCancel={() => {
            setDiscarding(null)
            setDiscardError('')
          }}
        />
      )}

      {confirmCancel && (
        <ConfirmDialog
          title="¿Cancelar la subida?"
          message="Se descartará lo que ya se subió de este archivo."
          confirmLabel="Cancelar subida"
          cancelLabel="Seguir subiendo"
          danger
          onConfirm={cancelUpload}
          onCancel={() => setConfirmCancel(false)}
        />
      )}
    </section>
  )
}
