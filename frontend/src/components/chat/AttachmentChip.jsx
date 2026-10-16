import { useState } from 'react'
import { ArrowDownTrayIcon, DocumentTextIcon, TableCellsIcon } from '@heroicons/react/24/outline'
import { openAttachment } from '../../lib/upload.js'
import { formatSize } from '../../lib/bitacora.js'

const SHEET_MIMES = new Set([
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel',
  'text/csv',
])

function fileKind({ mime = '', filename = '' }) {
  if (mime === 'application/pdf' || /\.pdf$/i.test(filename)) return 'pdf'
  if (SHEET_MIMES.has(mime) || /\.(xlsx|xls|csv)$/i.test(filename)) return 'sheet'
  return 'file'
}

/**
 * A file the chat generated (PDF / Excel). Tapping asks for a fresh presigned
 * URL right then (never stored): a PDF opens in a new tab, anything else is
 * downloaded.
 */
export default function AttachmentChip({ attachment }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const kind = fileKind(attachment)
  const Icon = kind === 'sheet' ? TableCellsIcon : DocumentTextIcon
  const label = kind === 'pdf' ? 'PDF' : kind === 'sheet' ? 'Excel' : 'Archivo'

  async function open() {
    setBusy(true)
    setFailed(false)
    try {
      await openAttachment(attachment.id, { download: kind !== 'pdf' })
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={open}
        disabled={busy}
        className="flex min-h-12 max-w-full items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-left active:bg-slate-50 disabled:opacity-60"
      >
        <Icon
          className={`size-7 shrink-0 ${kind === 'sheet' ? 'text-emerald-600' : 'text-red-600'}`}
          aria-hidden="true"
        />
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-slate-900">
            {attachment.filename}
          </span>
          <span className="block text-xs text-slate-500">
            {label}
            {attachment.size ? ` · ${formatSize(attachment.size)}` : ''}
          </span>
        </span>
        <ArrowDownTrayIcon className="ml-1 size-5 shrink-0 text-brand-700" aria-hidden="true" />
        <span className="sr-only">{kind === 'pdf' ? 'Abrir' : 'Descargar'}</span>
      </button>
      {failed && (
        <p role="alert" className="mt-1 text-xs text-red-700">
          No se pudo abrir el archivo. Inténtalo de nuevo.
        </p>
      )}
    </div>
  )
}
