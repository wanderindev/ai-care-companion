import { Link } from 'react-router-dom'
import { PaperClipIcon } from '@heroicons/react/20/solid'
import { entrySource, formatDate, formatTime, plainPreview } from '../../lib/bitacora.js'
import { PatientTag, ReviewBadge, TypeChip } from './EntryBadges.jsx'

/** Mobile list card; the whole card opens the entry. */
export default function EntryCard({ entry, patientName, linkState }) {
  const source = entrySource(entry)
  const preview = plainPreview(entry.indications || entry.family_notes)
  const files = entry.attachments.length

  return (
    <Link
      to={`/bitacora/${entry.id}`}
      state={linkState}
      className="block rounded-2xl border border-slate-200 bg-white p-4 shadow-sm active:bg-slate-50"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-slate-500">
        <time dateTime={entry.entry_date} className="font-medium text-slate-700">
          {formatDate(entry.entry_date)}
        </time>
        {entry.entry_time && <span>{formatTime(entry.entry_time)}</span>}
        <PatientTag name={patientName} />
        <span className="ml-auto flex items-center gap-1">
          {entry.review_status === 'pending_review' && <ReviewBadge />}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <TypeChip type={entry.entry_type} />
        {source && <span className="min-w-0 truncate font-medium text-slate-800">{source}</span>}
      </div>
      {entry.title && <p className="mt-1 text-base font-semibold text-slate-900">{entry.title}</p>}
      {preview && <p className="mt-1 line-clamp-3 text-sm text-slate-600">{preview}</p>}
      {files > 0 && (
        <p className="mt-2 flex items-center gap-1 text-sm text-slate-500">
          <PaperClipIcon className="size-4" aria-hidden="true" />
          {files === 1 ? '1 adjunto' : `${files} adjuntos`}
        </p>
      )}
    </Link>
  )
}
