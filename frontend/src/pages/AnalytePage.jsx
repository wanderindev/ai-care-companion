import { useId, useMemo } from 'react'
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeftIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { usePatient } from '../contexts/PatientContext.js'
import { useAnalyteSeries } from '../lib/labReports.js'
import { errorMessage, formatDate } from '../lib/bitacora.js'
import { groupByUnit, OUT_OF_RANGE, referenceText, resultValue } from '../lib/labs.js'
import { ErrorState } from '../components/bitacora/ListStates.jsx'
import { FlagBadge } from '../components/labs/LabBadges.jsx'
import TrendChart, { TrendLegend } from '../components/labs/TrendChart.jsx'

function positiveInt(value) {
  const n = Number(value)
  return Number.isInteger(n) && n > 0 ? n : null
}

/** Every result over time, newest first; each date opens its report. */
function HistoryTable({ id, points, linkState }) {
  const rows = [...points].reverse()
  return (
    <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
      <table id={id} className="w-full text-left text-sm">
        <caption className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-left font-semibold text-slate-700">
          Todos los resultados
        </caption>
        <thead>
          <tr className="border-b border-slate-200 text-xs text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2 font-semibold">
              Fecha
            </th>
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              Resultado
            </th>
            <th scope="col" className="hidden px-3 py-2 font-semibold sm:table-cell">
              Referencia
            </th>
            <th scope="col" className="px-3 py-2 font-semibold">
              <span className="sr-only sm:not-sr-only">Indicador</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((p) => {
            const ref = referenceText(p)
            return (
              <tr key={p.result_id} className={OUT_OF_RANGE.has(p.flag) ? 'bg-red-50/40' : undefined}>
                <th scope="row" className="px-3 py-2 align-top font-normal">
                  <Link
                    to={`/labs/${p.report_id}`}
                    state={linkState}
                    className="-my-1 flex min-h-11 flex-col justify-center font-medium text-brand-800 underline underline-offset-2"
                  >
                    <time dateTime={p.date}>{formatDate(p.date)}</time>
                  </Link>
                  {p.lab_name && <span className="block text-xs text-slate-500">{p.lab_name}</span>}
                </th>
                <td className="px-3 py-2.5 text-right align-top whitespace-nowrap tabular-nums">
                  <span className="font-semibold text-slate-900">{resultValue(p)}</span>
                  {p.unit && <span className="block text-xs text-slate-500">{p.unit}</span>}
                  {ref && (
                    <span className="block text-xs text-slate-500 sm:hidden">Ref.: {ref}</span>
                  )}
                </td>
                <td className="hidden px-3 py-2.5 align-top text-slate-600 tabular-nums sm:table-cell">
                  {ref || '—'}
                </td>
                <td className="px-3 py-2.5 align-top">
                  <FlagBadge flag={p.flag} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}

/**
 * One analyte for one patient (`?paciente=<id>`): a trend chart per unit with
 * the reference band, and the full table. Opened from a result in a report.
 */
export default function AnalytePage() {
  const { analyteId } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  const tableId = useId()
  const { patients, selectedPatients } = usePatient()
  // From the URL; else the switcher's patient when it shows only one.
  const patientId =
    positiveInt(searchParams.get('paciente')) ??
    (selectedPatients.length === 1 ? selectedPatients[0].id : null)
  const series = useAnalyteSeries(analyteId, patientId)
  const groups = useMemo(() => groupByUnit(series.data?.points ?? []), [series.data])

  const backTo = /^\/labs\/\d+$/.test(location.state?.backTo ?? '') ? location.state.backTo : null
  const backUrl = backTo ?? `/labs${location.state?.from ?? ''}`
  const linkState = { from: location.state?.from }
  const patientName = patients.find((p) => p.id === patientId)?.display_name

  const backLink = (
    <Link
      to={backUrl}
      className="-ml-2 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 font-medium text-brand-700 active:bg-brand-50"
    >
      <ArrowLeftIcon className="size-5 shrink-0" aria-hidden="true" />
      {backTo ? 'Informe' : 'Laboratorios'}
    </Link>
  )

  const patientPicker = (
    <div role="group" aria-label="Paciente" className="flex gap-2">
      {patients.map((p) => (
        <button
          key={p.id}
          type="button"
          aria-pressed={p.id === patientId}
          onClick={() =>
            setSearchParams({ paciente: String(p.id) }, { replace: true, state: location.state })
          }
          className={`min-h-11 rounded-full px-4 text-sm font-medium ring-1 ring-inset ${
            p.id === patientId
              ? 'bg-brand-700 text-white ring-brand-700'
              : 'bg-white text-slate-700 ring-slate-300 active:bg-slate-100'
          }`}
        >
          {p.display_name}
        </button>
      ))}
    </div>
  )

  if (!patientId) {
    return (
      <div className="space-y-4">
        {backLink}
        <p className="text-slate-700">¿De quién quieres ver este examen?</p>
        {patientPicker}
      </div>
    )
  }

  if (series.isPending || series.isError) {
    const notFound = series.error?.status === 404 || series.error?.status === 422
    return (
      <div>
        {backLink}
        <div className="mt-3">
          {series.isPending ? (
            <div aria-hidden="true" className="animate-pulse space-y-4">
              <div className="h-7 w-1/2 rounded bg-slate-200" />
              <div className="h-60 rounded-2xl bg-slate-100" />
            </div>
          ) : (
            <ErrorState
              message={
                notFound
                  ? 'Este examen no existe.'
                  : errorMessage(series.error, 'No se pudo cargar el examen.')
              }
              onRetry={notFound ? undefined : () => series.refetch()}
            />
          )}
        </div>
      </div>
    )
  }

  const { analyte, points } = series.data
  // The band follows each date's own range; say so when it moves.
  const rangesChange = groups.some(
    (g) =>
      new Set(
        g.points
          .filter((p) => p.ref_low !== null || p.ref_high !== null)
          .map((p) => `${p.ref_low}|${p.ref_high}`),
      ).size > 1,
  )

  return (
    <article className="space-y-4">
      {backLink}
      <header className="space-y-3">
        <div>
          <h1 className="text-2xl font-semibold break-words">{analyte.canonical_name}</h1>
          {analyte.aliases.length > 0 && (
            <p className="mt-0.5 text-sm text-slate-500">También: {analyte.aliases.join(', ')}</p>
          )}
        </div>
        {patients.length > 1 && patientPicker}
      </header>

      {points.length === 0 ? (
        <p className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center text-slate-700">
          {patientName ? `${patientName} no tiene resultados de este examen.` : 'Sin resultados.'}
        </p>
      ) : (
        <>
          {groups.length > 1 && (
            <p
              role="note"
              className="flex gap-2 rounded-2xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950"
            >
              <ExclamationTriangleIcon className="size-5 shrink-0" aria-hidden="true" />
              Este examen aparece con unidades distintas según el laboratorio. Para no mezclarlas,
              hay un gráfico por unidad.
            </p>
          )}
          {groups.map((group) => {
            const unitLabel = group.unit || 'sin unidad'
            return (
              <section
                key={group.key}
                className="rounded-2xl border border-slate-200 bg-white p-3 sm:p-4"
              >
                <h2 className="mb-2 text-sm font-semibold text-slate-700">
                  {groups.length > 1 ? `En ${unitLabel}` : `Evolución (${unitLabel})`}
                </h2>
                <TrendChart
                  points={group.points}
                  unit={group.unit}
                  label={`Evolución de ${analyte.canonical_name}${
                    patientName ? ` de ${patientName}` : ''
                  } en ${unitLabel}`}
                  describedBy={tableId}
                />
                <TrendLegend points={group.points} />
              </section>
            )
          })}
          {rangesChange && (
            <p className="text-sm text-slate-500">
              El rango de referencia cambia entre laboratorios; la franja sigue el de cada fecha.
            </p>
          )}
          <HistoryTable id={tableId} points={points} linkState={linkState} />
        </>
      )}
    </article>
  )
}
