import { useId, useState } from 'react'
import {
  ArrowTopRightOnSquareIcon,
  ArrowUpTrayIcon,
  ChevronDownIcon,
  LinkIcon,
} from '@heroicons/react/24/outline'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import { errorMessage } from '../../lib/bitacora.js'
import {
  formatSentAt,
  linkReasonLabel,
  safeHref,
  useIgnoreLabLink,
  usePendingLabLinks,
} from '../../lib/labLinks.js'

function LinkItem({ link, patientName, onUpload, onIgnore }) {
  const href = safeHref(link.url)
  const meta = [formatSentAt(link.sent_at), link.sender].filter(Boolean).join(' · ')
  return (
    <li className="p-4">
      <p className="break-all font-semibold text-slate-900">{link.host}</p>
      {meta && <p className="mt-0.5 text-sm text-slate-500">{meta}</p>}
      {patientName && <p className="mt-0.5 text-sm text-slate-600">Parece de {patientName}</p>}
      <p className="mt-1 text-sm text-amber-900">{linkReasonLabel(link.reason)}</p>
      <div className="mt-3 grid grid-cols-3 gap-2">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="flex min-h-12 items-center justify-center gap-1.5 rounded-2xl bg-white px-2 text-center text-sm font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100"
          >
            <ArrowTopRightOnSquareIcon className="size-5 shrink-0" aria-hidden="true" />
            Abrir enlace
          </a>
        ) : (
          <span />
        )}
        <button
          type="button"
          onClick={() => onUpload(link)}
          className="flex min-h-12 items-center justify-center gap-1.5 rounded-2xl bg-brand-700 px-2 text-sm font-semibold text-white active:bg-brand-800"
        >
          <ArrowUpTrayIcon className="size-5 shrink-0" aria-hidden="true" />
          Subir PDF
        </button>
        <button
          type="button"
          onClick={() => onIgnore(link)}
          className="min-h-12 rounded-2xl bg-white px-2 text-sm font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
        >
          Ignorar
        </button>
      </div>
    </li>
  )
}

/**
 * «Enlaces pendientes»: lab portal links shared in the chat that were not
 * downloaded automatically. Open the link, download the PDF(s) and «Subir PDF»
 * (the same reading as «Subir resultado»; several PDFs, one report each), or
 * «Ignorar» it. The patient is
 * only a guess from the message text; links with no guess show for everyone.
 * The URL is only an href (http/https) and plain text, never HTML.
 */
export default function PendingLinks({ patientIds, patients, onUpload }) {
  const query = usePendingLabLinks()
  const ignore = useIgnoreLabLink()
  const [open, setOpen] = useState(false)
  const [ignoring, setIgnoring] = useState(null)
  const panelId = useId()

  const items = (query.data ?? []).filter(
    (link) => link.patient_id === null || patientIds.includes(link.patient_id),
  )
  if (!items.length) return null
  const names = Object.fromEntries(patients.map((p) => [p.id, p.display_name]))

  function confirmIgnore() {
    ignore.mutate(ignoring.id, { onSuccess: () => setIgnoring(null) })
  }

  return (
    <section className="mb-4 rounded-2xl border border-slate-200 bg-white">
      <h2>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((v) => !v)}
          className="flex min-h-12 w-full items-center gap-3 px-4 py-2 text-left"
        >
          <LinkIcon className="size-6 shrink-0 text-brand-700" aria-hidden="true" />
          <span className="flex-1 font-medium text-slate-900">
            Enlaces pendientes ({items.length})
          </span>
          <ChevronDownIcon
            className={`size-5 text-slate-500 transition-transform ${open ? 'rotate-180' : ''}`}
            aria-hidden="true"
          />
        </button>
      </h2>
      {open && (
        <div id={panelId}>
          <p className="px-4 text-sm text-slate-600">
            Enlaces a resultados que llegaron por WhatsApp. Ábrelo, descarga los PDF y súbelos con
            «Subir PDF» (puedes elegir varios a la vez).
          </p>
          <ul className="divide-y divide-slate-100">
            {items.map((link) => (
              <LinkItem
                key={link.id}
                link={link}
                patientName={link.patient_id !== null ? names[link.patient_id] : null}
                onUpload={onUpload}
                onIgnore={(l) => {
                  ignore.reset()
                  setIgnoring(l)
                }}
              />
            ))}
          </ul>
        </div>
      )}
      {ignoring && (
        <ConfirmDialog
          title="¿Ignorar este enlace?"
          message={`El enlace de ${ignoring.host} dejará de aparecer en la lista.`}
          confirmLabel="Ignorar"
          busy={ignore.isPending}
          error={ignore.isError ? errorMessage(ignore.error, 'No se pudo ignorar el enlace.') : ''}
          onConfirm={confirmIgnore}
          onCancel={() => setIgnoring(null)}
        />
      )}
    </section>
  )
}
