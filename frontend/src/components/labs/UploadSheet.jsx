import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  CameraIcon,
  CheckCircleIcon,
  DocumentTextIcon,
  ExclamationTriangleIcon,
  PhotoIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import Sheet from '../ui/Sheet.jsx'
import JobProgress from '../ui/JobProgress.jsx'
import { usePatient } from '../../contexts/PatientContext.js'
import { errorMessage, formatSize } from '../../lib/bitacora.js'
import { isJobFinished, useJob } from '../../lib/jobs.js'
import { LAB_REPORTS_KEY } from '../../lib/labReports.js'
import { LAB_LINKS_KEY } from '../../lib/labLinks.js'
import {
  MAX_LINK_PDFS,
  MAX_PHOTOS,
  PICKER_ACCEPT,
  checkLabFiles,
  isPdf,
  jobErrorMessage,
  prepareLabFiles,
  startLabExtraction,
  useLabUploadOutcome,
} from '../../lib/labUpload.js'
import { uploadAttachment } from '../../lib/upload.js'

const UNKNOWN = 'no-se'

function PatientChoice({ value, onChange, patients }) {
  const name = useId()
  const options = [
    ...patients.map((p) => ({ value: String(p.id), label: p.display_name })),
    { value: UNKNOWN, label: 'No sé' },
  ]
  return (
    <fieldset className="mt-5">
      <legend className="text-sm font-medium text-slate-700">¿De quién es?</legend>
      <div className="mt-2 grid grid-cols-3 gap-2">
        {options.map((o) => (
          <label
            key={o.value}
            className={`flex min-h-12 cursor-pointer items-center justify-center rounded-2xl px-2 text-center font-medium ring-1 ${
              value === o.value
                ? 'bg-brand-700 text-white ring-brand-700'
                : 'bg-white text-slate-700 ring-slate-300 active:bg-slate-100'
            }`}
          >
            <input
              type="radio"
              name={name}
              value={o.value}
              checked={value === o.value}
              onChange={() => onChange(o.value)}
              className="sr-only"
            />
            {o.label}
          </label>
        ))}
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Solo es una pista: si el documento muestra otro nombre o cédula, no se usa. Si el
        paciente se elige solo por esta pista, el informe quedará por revisar para que alguien
        lo confirme.
      </p>
    </fieldset>
  )
}

function FileList({ files, onRemove }) {
  const photos = !files.some(isPdf)
  return (
    <ol className="mt-4 divide-y divide-slate-100 rounded-2xl border border-slate-200">
      {files.map((file, index) => (
        <li key={`${file.name}-${file.lastModified}-${index}`} className="flex items-center gap-3 p-2 pl-3">
          {isPdf(file) ? (
            <DocumentTextIcon className="size-7 shrink-0 text-slate-400" aria-hidden="true" />
          ) : (
            <PhotoIcon className="size-7 shrink-0 text-slate-400" aria-hidden="true" />
          )}
          <div className="min-w-0 flex-1">
            <p className="truncate font-medium">
              {photos ? `Página ${index + 1}` : file.name || 'PDF'}
            </p>
            <p className="truncate text-sm text-slate-500">
              {photos && file.name ? `${file.name} · ` : ''}
              {formatSize(file.size)}
            </p>
          </div>
          <button
            type="button"
            onClick={() => onRemove(index)}
            aria-label={`Quitar ${photos ? `página ${index + 1}` : file.name || 'el PDF'}`}
            className="flex size-11 shrink-0 items-center justify-center rounded-full text-slate-500 active:bg-slate-100"
          >
            <XMarkIcon className="size-5" aria-hidden="true" />
          </button>
        </li>
      ))}
    </ol>
  )
}

/**
 * Why a finished upload job has no report: `{ message, retryable }`.
 * Not a lab report, or a file already read, is not worth another try.
 */
function outcomeProblem(job, outcome) {
  const result = outcome.data
  if (result?.outcome === 'not_lab_report') {
    return {
      message: 'No parece un resultado de laboratorio. Revisa que sea el informe con los valores.',
      retryable: false,
    }
  }
  if (result?.outcome === 'lab_report') {
    return {
      message:
        'Este archivo ya se había leído y su informe se eliminó, así que no se vuelve a leer.',
      retryable: false,
    }
  }
  if (outcome.isError) {
    return {
      message: errorMessage(outcome.error, 'No se pudo consultar el resultado.'),
      retryable: true,
    }
  }
  return {
    message: jobErrorMessage(job, 'No se pudo leer el resultado automáticamente.'),
    retryable: true,
  }
}

/**
 * One PDF of a lab link upload with several PDFs (#124): polls its own job
 * and reports where it ended (`onEnd(key, { reportId } | { error, retryable })`).
 */
function PdfProgress({ item, onEnd, onRetry, onOpen }) {
  const reading = item.state === 'reading'
  const { data: job } = useJob(reading ? item.jobId : null)
  const finished = reading && isJobFinished(job)
  const outcome = useLabUploadOutcome(reading ? item.jobId : null, finished)
  const ended = finished && !outcome.isPending

  useEffect(() => {
    if (!ended) return
    if (outcome.data?.report_id) onEnd(item.key, { reportId: outcome.data.report_id })
    else {
      const problem = outcomeProblem(job, outcome)
      onEnd(item.key, { error: problem.message, retryable: problem.retryable })
    }
  }, [ended, item.key, job, outcome, onEnd])

  return (
    <li className="flex items-start gap-3 p-3">
      {item.state === 'done' ? (
        <CheckCircleIcon className="mt-0.5 size-7 shrink-0 text-brand-700" aria-hidden="true" />
      ) : item.state === 'error' ? (
        <ExclamationTriangleIcon
          className="mt-0.5 size-7 shrink-0 text-amber-700"
          aria-hidden="true"
        />
      ) : (
        <DocumentTextIcon className="mt-0.5 size-7 shrink-0 text-slate-400" aria-hidden="true" />
      )}
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{item.file.name || 'PDF'}</p>
        {item.state === 'waiting' && <p className="text-sm text-slate-500">En espera</p>}
        {(item.state === 'uploading' || item.state === 'reading') && (
          <p className="text-sm text-slate-600">Leyendo…</p>
        )}
        {item.state === 'done' && (
          <p className="flex flex-wrap items-center gap-x-3 text-sm text-slate-700">
            <span className="font-medium text-brand-800">Listo</span>
            <button
              type="button"
              onClick={() => onOpen(item.reportId)}
              className="min-h-11 font-medium text-brand-700 underline underline-offset-2"
            >
              Ver informe
            </button>
          </p>
        )}
        {item.state === 'error' && (
          <>
            <p role="alert" className="text-sm text-amber-950">
              {item.error}
            </p>
            {item.retryable && (
              <button
                type="button"
                onClick={() => onRetry(item.key)}
                className="mt-2 min-h-11 rounded-2xl bg-amber-900 px-4 font-semibold text-white active:bg-amber-950"
              >
                Reintentar
              </button>
            )}
          </>
        )}
      </div>
    </li>
  )
}

/** Where the job ended: navigate to the report, or say why not. */
function JobResult({ jobId, onDone, onRetry }) {
  const { data: job } = useJob(jobId)
  const finished = isJobFinished(job)
  const outcome = useLabUploadOutcome(jobId, finished)
  const result = outcome.data
  const opened = useRef(false)

  useEffect(() => {
    if (result?.report_id && !opened.current) {
      opened.current = true
      onDone(result.report_id)
    }
  }, [result?.report_id, onDone])

  if (!finished || outcome.isPending || result?.report_id) return null
  const { message } = outcomeProblem(job, outcome)
  return (
    <div className="mt-4 rounded-2xl border border-amber-300 bg-amber-50 p-4">
      <p role="alert" className="font-medium text-amber-950">
        {message}
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 min-h-12 rounded-2xl bg-amber-900 px-4 font-semibold text-white active:bg-amber-950"
      >
        Subir otro
      </button>
    </div>
  )
}

/**
 * «Subir resultado»: one PDF, or photos of the pages of one report (camera
 * or gallery), uploaded and read by the same extraction as WhatsApp PDFs.
 * Any family member can upload. On success it opens the new report.
 * `labLinkId`: «Subir PDF» from a pending lab link; the link is resolved.
 * A link may hold several PDFs (#124): in that mode several can be picked,
 * each uploaded and read as its own report (one request and job per PDF,
 * never merged), with progress per file and a summary at the end. Photos
 * stay one report.
 */
export default function UploadSheet({ onClose, labLinkId = null }) {
  const { patients } = usePatient()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [files, setFiles] = useState([])
  // No hint unless the uploader picks one here: the header's patient may not
  // be the one on this report.
  const [hint, setHint] = useState(UNKNOWN)
  const [preparing, setPreparing] = useState(false)
  const [phase, setPhase] = useState('pick') // pick | uploading | job | multi
  // Link mode with several PDFs: one entry per file, `{ key, file, state:
  // waiting|uploading|reading|done|error, jobId, reportId, error, retryable }`.
  const [items, setItems] = useState([])
  const [uploaded, setUploaded] = useState(0)
  const [error, setError] = useState('')
  const [jobId, setJobId] = useState(null)
  const cameraRef = useRef(null)
  const pickerRef = useRef(null)
  const abortRef = useRef(null)

  useEffect(() => () => abortRef.current?.abort(), [])

  const linkMode = labLinkId != null
  const problem = files.length ? checkLabFiles(files, { multiplePdfs: linkMode }) : ''

  async function addFiles(list) {
    const picked = Array.from(list ?? [])
    if (!picked.length) return
    setError('')
    setPreparing(true)
    const added = await prepareLabFiles(picked)
    setPreparing(false)
    // A PDF replaces everything (link mode: PDFs add up, one report each);
    // photos are appended as the next pages.
    setFiles((current) => {
      if (!added.some(isPdf)) return [...current.filter((f) => !isPdf(f)), ...added]
      const pdfs = added.filter(isPdf)
      if (!linkMode) return pdfs.slice(0, 1)
      const kept = current.filter(isPdf)
      const same = (a, b) =>
        a.name === b.name && a.size === b.size && a.lastModified === b.lastModified
      return [...kept, ...pdfs.filter((f) => !kept.some((k) => same(k, f)))]
    })
  }

  const patchItem = useCallback(
    (key, values) =>
      setItems((current) => current.map((i) => (i.key === key ? { ...i, ...values } : i))),
    [],
  )

  // One PDF at a time: upload, then its own extraction request and job.
  async function runItems(list) {
    if (!abortRef.current || abortRef.current.signal.aborted) {
      abortRef.current = new AbortController()
    }
    const { signal } = abortRef.current
    for (const item of list) {
      patchItem(item.key, { state: 'uploading', error: '', jobId: null })
      try {
        const attachment = await uploadAttachment(item.file, { signal })
        const { job_id: id } = await startLabExtraction(
          [attachment.id],
          hint === UNKNOWN ? null : Number(hint),
          labLinkId,
        )
        queryClient.invalidateQueries({ queryKey: LAB_LINKS_KEY })
        patchItem(item.key, { state: 'reading', jobId: id })
      } catch (e) {
        if (signal.aborted) return
        patchItem(item.key, {
          state: 'error',
          error: errorMessage(e, 'No se pudo subir este PDF.'),
          retryable: true,
        })
      }
    }
  }

  const endItem = useCallback(
    (key, { reportId = null, error = '', retryable = false }) => {
      if (reportId) queryClient.invalidateQueries({ queryKey: LAB_REPORTS_KEY })
      patchItem(key, reportId ? { state: 'done', reportId } : { state: 'error', error, retryable })
    },
    [patchItem, queryClient],
  )

  function retryItem(key) {
    const item = items.find((i) => i.key === key)
    if (item) runItems([item])
  }

  function submitMany() {
    const list = files.map((file, index) => ({
      key: `${index}-${file.name}-${file.size}-${file.lastModified}`,
      file,
      state: 'waiting',
      jobId: null,
      reportId: null,
      error: '',
      retryable: false,
    }))
    setItems(list)
    setError('')
    setPhase('multi')
    runItems(list)
  }

  async function submit() {
    if (problem || !files.length) return
    if (linkMode && files.length > 1 && files.every(isPdf)) {
      submitMany()
      return
    }
    setPhase('uploading')
    setUploaded(0)
    setError('')
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const ids = []
      // One at a time, in page order.
      for (const file of files) {
        const attachment = await uploadAttachment(file, { signal: controller.signal })
        ids.push(attachment.id)
        setUploaded(ids.length)
      }
      const { job_id: id } = await startLabExtraction(
        ids,
        hint === UNKNOWN ? null : Number(hint),
        labLinkId,
      )
      if (labLinkId != null) queryClient.invalidateQueries({ queryKey: LAB_LINKS_KEY })
      setJobId(id)
      setPhase('job')
    } catch (e) {
      if (controller.signal.aborted) return
      setError(errorMessage(e, 'No se pudo subir el resultado.'))
      setPhase('pick')
    }
  }

  function openReport(reportId) {
    queryClient.invalidateQueries({ queryKey: LAB_REPORTS_KEY })
    onClose()
    navigate(`/labs/${reportId}`)
  }

  function reset() {
    queryClient.invalidateQueries({ queryKey: LAB_REPORTS_KEY })
    setFiles([])
    setJobId(null)
    setPhase('pick')
  }

  function goToLabs() {
    queryClient.invalidateQueries({ queryKey: LAB_REPORTS_KEY })
    onClose()
    navigate('/labs')
  }

  const multiEnded =
    phase === 'multi' && items.every((i) => i.state === 'done' || i.state === 'error')
  const doneCount = items.filter((i) => i.state === 'done').length
  const busy = phase === 'uploading' || items.some((i) => i.state === 'uploading')
  const multiPdfs = linkMode && files.length > 1 && files.every(isPdf)
  const footer =
    phase === 'multi' ? (
      multiEnded ? (
        <button
          type="button"
          onClick={goToLabs}
          className="min-h-12 w-full rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
        >
          Ir a Laboratorios
        </button>
      ) : null
    ) : phase === 'pick' ? (
      <button
        type="button"
        onClick={submit}
        disabled={!files.length || Boolean(problem) || preparing}
        className="min-h-12 w-full rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800 disabled:opacity-50"
      >
        {multiPdfs ? `Leer ${files.length} PDF` : 'Leer resultado'}
      </button>
    ) : null

  return (
    <Sheet
      title={labLinkId != null ? 'Subir PDF del enlace' : 'Subir resultado'}
      onClose={busy ? () => {} : onClose}
      footer={footer}
    >
      {phase === 'multi' ? (
        <>
          <p className="text-slate-700" aria-live="polite">
            {multiEnded
              ? `Listo: se leyeron ${doneCount} de ${items.length} PDF.`
              : `Leyendo ${items.length} PDF, cada uno como un informe aparte. Puede tardar unos minutos.`}
          </p>
          <ol className="mt-4 divide-y divide-slate-100 rounded-2xl border border-slate-200">
            {items.map((item) => (
              <PdfProgress
                key={item.key}
                item={item}
                onEnd={endItem}
                onRetry={retryItem}
                onOpen={openReport}
              />
            ))}
          </ol>
          {!multiEnded && (
            <p className="mt-3 text-sm text-slate-600">
              Si cierras esta ventana después de subirlos, los informes aparecerán igual en la
              lista cuando estén listos.
            </p>
          )}
        </>
      ) : phase === 'job' ? (
        <>
          <JobProgress jobId={jobId} title="Leyendo el resultado" />
          <p className="mt-3 text-sm text-slate-600">
            Puede tardar un par de minutos. Si cierras esta ventana, el informe aparecerá igual en
            la lista cuando esté listo.
          </p>
          <JobResult jobId={jobId} onDone={openReport} onRetry={reset} />
        </>
      ) : (
        <>
          <p className="text-slate-700">
            {linkMode
              ? 'Sube los PDF que descargaste del enlace del laboratorio (cada PDF se lee como un informe aparte), o fotos de cada página de un informe, en orden.'
              : 'Sube el PDF del laboratorio o toma fotos de cada página del informe, en orden.'}
          </p>
          {linkMode && (
            <p className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-950">
              Si el portal tiene un PDF consolidado del día, súbelo solo a él: si subes también
              los PDF de cada examen, los resultados quedan repetidos en las gráficas.
            </p>
          )}
          <div className="mt-4 grid grid-cols-2 gap-2">
            <button
              type="button"
              disabled={busy || preparing}
              onClick={() => cameraRef.current?.click()}
              className="flex min-h-14 items-center justify-center gap-2 rounded-2xl bg-white px-3 font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
            >
              <CameraIcon className="size-6" aria-hidden="true" />
              Tomar foto
            </button>
            <button
              type="button"
              disabled={busy || preparing}
              onClick={() => pickerRef.current?.click()}
              className="flex min-h-14 items-center justify-center gap-2 rounded-2xl bg-white px-3 font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
            >
              <DocumentTextIcon className="size-6" aria-hidden="true" />
              PDF o galería
            </button>
          </div>
          <input
            ref={cameraRef}
            type="file"
            accept="image/*"
            capture="environment"
            className="hidden"
            onChange={(e) => {
              addFiles(e.target.files)
              e.target.value = ''
            }}
          />
          <input
            ref={pickerRef}
            type="file"
            accept={PICKER_ACCEPT}
            multiple
            className="hidden"
            onChange={(e) => {
              addFiles(e.target.files)
              e.target.value = ''
            }}
          />
          {files.length > 0 && (
            <FileList
              files={files}
              onRemove={(index) => setFiles((current) => current.filter((_, i) => i !== index))}
            />
          )}
          {linkMode && files.length > 0 && files.every(isPdf) && files.length < MAX_LINK_PDFS && (
            <p className="mt-2 text-sm text-slate-500">
              ¿El enlace tiene más PDF? Elígelos con «PDF o galería».
            </p>
          )}
          {files.length > 0 && !files.some(isPdf) && files.length < MAX_PHOTOS && (
            <p className="mt-2 text-sm text-slate-500">
              ¿Tiene más páginas? Toma o elige la siguiente foto.
            </p>
          )}
          {preparing && (
            <p className="mt-3 text-sm text-slate-600" aria-live="polite">
              Preparando las fotos…
            </p>
          )}
          {problem && (
            <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
              {problem}
            </p>
          )}
          <PatientChoice value={hint} onChange={setHint} patients={patients} />
          {busy && (
            <p className="mt-4 text-sm text-slate-600" aria-live="polite">
              Subiendo {uploaded + 1 > files.length ? files.length : uploaded + 1} de{' '}
              {files.length}…
            </p>
          )}
          {error && (
            <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
              {error}
            </p>
          )}
        </>
      )}
    </Sheet>
  )
}
