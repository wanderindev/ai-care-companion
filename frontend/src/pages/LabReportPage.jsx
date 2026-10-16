import { useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowDownTrayIcon,
  ArrowLeftIcon,
  ArrowTopRightOnSquareIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  DocumentTextIcon,
  PencilSquareIcon,
  PhotoIcon,
  TrashIcon,
} from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { useMe } from '../lib/auth.js'
import { useDeleteLabReport, useLabReport, useUpdateLabReport } from '../lib/labReports.js'
import { openAttachment } from '../lib/upload.js'
import { errorMessage, formatDate, formatLongDate, formatSize } from '../lib/bitacora.js'
import { groupByPanel, OUT_OF_RANGE, referenceText, resultValue, sourceLabel } from '../lib/labs.js'
import { ReviewBadge } from '../components/bitacora/EntryBadges.jsx'
import { ErrorState } from '../components/bitacora/ListStates.jsx'
import { FlagBadge, UnassignedBadge } from '../components/labs/LabBadges.jsx'
import ReportEditSheet from '../components/labs/ReportEditSheet.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'

function DetailSkeleton() {
  return (
    <div aria-hidden="true" className="animate-pulse space-y-4">
      <div className="h-7 w-2/3 rounded bg-slate-200" />
      <div className="h-4 w-1/2 rounded bg-slate-100" />
      <div className="h-64 rounded-2xl bg-slate-100" />
    </div>
  )
}

/**
 * The report's file — the PDF, or one photographed page — opens in a new tab
 * (or downloads) with a URL fetched on tap.
 */
function FileActions({ attachment, page }) {
  const [error, setError] = useState('')
  const isPdf = attachment.mime === 'application/pdf'
  const noun = isPdf ? 'PDF' : 'foto'
  const run = (options) =>
    openAttachment(attachment.id, options).catch((e) =>
      setError(errorMessage(e, `No se pudo abrir ${isPdf ? 'el PDF' : 'la foto'}.`)),
    )
  const buttonClass =
    'flex min-h-11 items-center gap-1.5 rounded-xl px-3 text-sm font-medium text-brand-700 ring-1 ring-slate-200 active:bg-brand-50'
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-center gap-3">
        {isPdf ? (
          <DocumentTextIcon className="size-8 shrink-0 text-slate-400" aria-hidden="true" />
        ) : (
          <PhotoIcon className="size-8 shrink-0 text-slate-400" aria-hidden="true" />
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">
            {page ? `Página ${page}` : attachment.filename}
          </p>
          <p className="truncate text-sm text-slate-500">
            {isPdf ? 'PDF' : 'Foto'} · {formatSize(attachment.size)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => run()} className={buttonClass}>
            <ArrowTopRightOnSquareIcon className="size-4" aria-hidden="true" />
            Ver {noun}
          </button>
          <button type="button" onClick={() => run({ download: true })} className={buttonClass}>
            <ArrowDownTrayIcon className="size-4" aria-hidden="true" />
            Descargar
          </button>
        </div>
      </div>
      {error && (
        <p role="alert" className="mt-2 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}
    </section>
  )
}

/**
 * Results of one panel. A result with a known analyte links to its trend
 * (only once the report has a patient). Phones show the reference under the
 * name; wider screens give it its own column.
 */
function ResultsTable({ panel, results, patientId, linkState }) {
  return (
    <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-left font-semibold text-slate-700">
          {panel || 'Resultados'}
        </caption>
        <thead className="sr-only sm:not-sr-only">
          <tr className="border-b border-slate-200 text-xs text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2 font-semibold">
              Examen
            </th>
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              Resultado
            </th>
            <th scope="col" className="hidden px-3 py-2 font-semibold sm:table-cell">
              Referencia
            </th>
            <th scope="col" className="px-3 py-2 font-semibold">
              Indicador
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {results.map((r) => {
            const ref = referenceText(r)
            const link = r.analyte_id && patientId
            const outside = OUT_OF_RANGE.has(r.flag)
            return (
              <tr key={r.id} className={outside ? 'bg-red-50/40' : undefined}>
                <th scope="row" className="px-3 py-2.5 align-top font-normal">
                  {link ? (
                    <Link
                      to={`/labs/examenes/${r.analyte_id}?paciente=${patientId}`}
                      state={linkState}
                      className="-my-1 flex min-h-11 items-center gap-1 font-medium text-brand-800 underline-offset-2 hover:underline"
                    >
                      <span className="min-w-0 break-words">{r.raw_name}</span>
                      <ChevronRightIcon className="size-4 shrink-0" aria-hidden="true" />
                    </Link>
                  ) : (
                    <span className="font-medium text-slate-900">{r.raw_name}</span>
                  )}
                  {ref && (
                    <span className="block text-xs text-slate-500 sm:hidden">Ref.: {ref}</span>
                  )}
                </th>
                <td className="px-3 py-2.5 text-right align-top whitespace-nowrap tabular-nums">
                  <span className="font-semibold text-slate-900">{resultValue(r)}</span>
                  {r.unit && <span className="block text-xs text-slate-500">{r.unit}</span>}
                </td>
                <td className="hidden px-3 py-2.5 align-top text-slate-600 tabular-nums sm:table-cell">
                  {ref || '—'}
                </td>
                <td className="px-3 py-2.5 align-top">
                  <FlagBadge flag={r.flag} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}

/** One lab report: header, review banner, files, results by panel, corrections. */
export default function LabReportPage() {
  const { reportId } = useParams()
  const location = useLocation()
  const { patients } = usePatient()
  const reportQuery = useLabReport(reportId)
  const markReviewed = useUpdateLabReport()
  const remove = useDeleteLabReport()
  const navigate = useNavigate()
  const { data: me } = useMe()
  const [editing, setEditing] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const listUrl = `/labs${location.state?.from ?? ''}`
  const report = reportQuery.data

  const backLink = (
    <Link
      to={listUrl}
      className="-ml-2 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 font-medium text-brand-700 active:bg-brand-50"
    >
      <ArrowLeftIcon className="size-5 shrink-0" aria-hidden="true" />
      Laboratorios
    </Link>
  )

  if (reportQuery.isPending || reportQuery.isError) {
    const notFound = reportQuery.error?.status === 404 || reportQuery.error?.status === 422
    return (
      <div>
        {backLink}
        <div className="mt-3">
          {reportQuery.isPending ? (
            <DetailSkeleton />
          ) : (
            <ErrorState
              message={
                notFound
                  ? 'Este informe no existe.'
                  : errorMessage(reportQuery.error, 'No se pudo cargar el informe.')
              }
              onRetry={notFound ? undefined : () => reportQuery.refetch()}
            />
          )}
        </div>
      </div>
    )
  }

  const patientName = patients.find((p) => p.id === report.patient_id)?.display_name
  const pending = report.status !== 'ok'
  const groups = groupByPanel(report.results)
  const files = report.attachments?.length
    ? report.attachments
    : [report.attachment].filter(Boolean)
  const resultsState = { from: location.state?.from, backTo: `/labs/${report.id}` }

  return (
    <article className="space-y-4">
      {backLink}

      <header>
        <div className="flex flex-wrap items-center gap-2">
          {report.patient_id === null ? (
            <UnassignedBadge />
          ) : (
            <span className="text-sm font-semibold text-brand-800">{patientName}</span>
          )}
          {pending && <ReviewBadge />}
        </div>
        <h1 className="mt-2 text-2xl font-semibold break-words">
          {report.lab_name || 'Laboratorio sin nombre'}
        </h1>
        <dl className="mt-1 space-y-0.5 text-sm text-slate-700">
          <div className="flex flex-wrap gap-1">
            <dt className="text-slate-500">Muestra:</dt>
            <dd className="first-letter:uppercase">
              {report.collected_on ? formatLongDate(report.collected_on) : 'sin fecha'}
            </dd>
          </div>
          {report.reported_on && (
            <div className="flex flex-wrap gap-1">
              <dt className="text-slate-500">Informe:</dt>
              <dd>{formatDate(report.reported_on)}</dd>
            </div>
          )}
          <div className="flex flex-wrap gap-1">
            <dt className="text-slate-500">Origen:</dt>
            <dd>{sourceLabel(report.source)}</dd>
          </div>
        </dl>
      </header>

      {pending && (
        <div className="rounded-2xl border border-amber-300 bg-amber-50 p-4">
          <p className="font-medium text-amber-950">Este informe está por revisar.</p>
          <p className="mt-1 text-sm text-amber-900">
            {report.patient_id === null
              ? 'No se pudo identificar al paciente. Abre el documento, corrige el informe y elige a quién pertenece.'
              : 'Se leyó automáticamente y algo no quedó claro. Compara los valores con el documento y márcalo como revisado.'}
          </p>
          {markReviewed.isError && (
            <p role="alert" className="mt-2 text-sm text-red-800">
              {errorMessage(markReviewed.error, 'No se pudo marcar como revisado.')}
            </p>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            {report.patient_id === null ? (
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="flex min-h-12 items-center gap-2 rounded-2xl bg-amber-900 px-4 font-semibold text-white active:bg-amber-950"
              >
                <PencilSquareIcon className="size-5" aria-hidden="true" />
                Elegir paciente
              </button>
            ) : (
              <button
                type="button"
                onClick={() => markReviewed.mutate({ id: report.id, data: { status: 'ok' } })}
                disabled={markReviewed.isPending}
                className="flex min-h-12 items-center gap-2 rounded-2xl bg-amber-900 px-4 font-semibold text-white active:bg-amber-950 disabled:opacity-60"
              >
                <CheckCircleIcon className="size-5" aria-hidden="true" />
                {markReviewed.isPending ? 'Guardando…' : 'Marcar como revisado'}
              </button>
            )}
          </div>
        </div>
      )}

      {files.map((attachment, index) => (
        <FileActions
          key={attachment.id}
          attachment={attachment}
          page={files.length > 1 ? index + 1 : null}
        />
      ))}

      {groups.length ? (
        groups.map((g) => (
          <ResultsTable
            key={g.panel ?? ''}
            panel={g.panel}
            results={g.results}
            patientId={report.patient_id}
            linkState={resultsState}
          />
        ))
      ) : (
        <p className="text-slate-500">Este informe no tiene resultados.</p>
      )}
      {report.patient_id === null && report.results.some((r) => r.analyte_id) && (
        <p className="text-sm text-slate-500">
          Cuando elijas el paciente podrás ver la evolución de cada examen.
        </p>
      )}

      <div className="pt-2">
        <button
          type="button"
          onClick={() => {
            markReviewed.reset()
            setEditing(true)
          }}
          className="flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl px-4 font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 sm:w-auto"
        >
          <PencilSquareIcon className="size-5" aria-hidden="true" />
          Corregir paciente, fechas o laboratorio
        </button>
      </div>

      {me?.is_admin && (
        <div className="border-t border-slate-200 pt-4">
          <p className="mb-2 text-sm text-slate-600">
            ¿Este informe no es de Papá ni de Mamá? Puedes eliminarlo.
          </p>
          <button
            type="button"
            onClick={() => {
              remove.reset()
              setConfirmingDelete(true)
            }}
            className="flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl px-4 font-medium text-red-700 ring-1 ring-red-200 active:bg-red-50 sm:w-auto"
          >
            <TrashIcon className="size-5" aria-hidden="true" />
            Eliminar informe
          </button>
        </div>
      )}

      {editing && (
        <ReportEditSheet
          report={report}
          onClose={() => setEditing(false)}
          onSaved={() => setEditing(false)}
        />
      )}

      {confirmingDelete && (
        <ConfirmDialog
          title="Eliminar informe"
          message="Se borrará el informe y sus resultados. El archivo original se conserva."
          confirmLabel={remove.isPending ? 'Eliminando…' : 'Eliminar informe'}
          danger
          busy={remove.isPending}
          error={
            remove.isError ? errorMessage(remove.error, 'No se pudo eliminar el informe.') : ''
          }
          onConfirm={() =>
            remove.mutate(report.id, {
              onSuccess: () => navigate(listUrl, { replace: true }),
            })
          }
          onCancel={() => setConfirmingDelete(false)}
        />
      )}
    </article>
  )
}
