import { Link } from 'react-router-dom'
import { DocumentTextIcon } from '@heroicons/react/20/solid'
import { ReviewBadge, PatientTag } from '../bitacora/EntryBadges.jsx'
import { UnassignedBadge } from './LabBadges.jsx'

function resultsLabel(n) {
  return n === 1 ? '1 resultado' : `${n} resultados`
}

/** One report in the Labs list; the whole card opens the report. */
export default function ReportCard({ report, patientName, linkState }) {
  const flagged = report.out_of_range_count
  const more = flagged - report.flagged_names.length
  return (
    <Link
      to={`/labs/${report.id}`}
      state={linkState}
      className="block rounded-2xl border border-slate-200 bg-white p-4 shadow-sm active:bg-slate-50"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 truncate font-semibold text-slate-900">
          {report.lab_name || 'Laboratorio sin nombre'}
        </span>
        {report.patient_id === null ? <UnassignedBadge /> : <PatientTag name={patientName} />}
        {report.status !== 'ok' && <ReviewBadge />}
      </div>
      {report.panels.length > 0 && (
        <p className="mt-1 line-clamp-2 text-sm text-slate-600">{report.panels.join(' · ')}</p>
      )}
      <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-500">
        <span>{resultsLabel(report.result_count)}</span>
        {report.attachment && (
          <span className="flex items-center gap-1">
            <DocumentTextIcon className="size-4" aria-hidden="true" />
            PDF
          </span>
        )}
      </p>
      {flagged > 0 && (
        <p className="mt-2 text-sm text-red-800">
          <span className="font-semibold">
            {flagged === 1 ? '1 fuera de rango' : `${flagged} fuera de rango`}:
          </span>{' '}
          {report.flagged_names.join(', ')}
          {more > 0 && ` y ${more} más`}
        </p>
      )}
    </Link>
  )
}
