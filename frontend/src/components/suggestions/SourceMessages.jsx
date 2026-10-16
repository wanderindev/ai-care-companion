import { useState } from 'react'
import { DocumentTextIcon, FilmIcon, PhotoIcon } from '@heroicons/react/24/outline'
import AudioMessage from '../media/AudioMessage.jsx'
import { getAttachmentUrl, openAttachment } from '../../lib/upload.js'
import { formatSentAt } from '../../lib/suggestions.js'

const fileButton =
  'inline-flex min-h-11 items-center gap-2 rounded-xl px-3 font-medium text-brand-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60'

/** A photo: the presigned URL is fetched only when the user asks to see it. */
function PhotoPreview({ attachmentId, label }) {
  const [url, setUrl] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    setLoading(true)
    setError('')
    try {
      setUrl((await getAttachmentUrl(attachmentId)).url)
    } catch {
      setError('No se pudo cargar la foto. Inténtalo de nuevo.')
    } finally {
      setLoading(false)
    }
  }

  if (url) {
    return (
      <img
        src={url}
        alt={label}
        // An expired URL: back to the button, which fetches a new one.
        onError={() => setUrl(null)}
        className="max-h-80 w-auto max-w-full rounded-xl ring-1 ring-slate-200"
      />
    )
  }
  return (
    <div>
      <button type="button" onClick={load} disabled={loading} className={fileButton}>
        <PhotoIcon className="size-5" aria-hidden="true" />
        {loading ? 'Cargando…' : 'Ver foto'}
      </button>
      {error && (
        <p role="alert" className="mt-1 text-sm text-red-700">
          {error}
        </p>
      )}
    </div>
  )
}

function PdfLink({ attachmentId, filename }) {
  const [error, setError] = useState('')
  async function open() {
    setError('')
    try {
      await openAttachment(attachmentId)
    } catch {
      setError('No se pudo abrir el documento. Inténtalo de nuevo.')
    }
  }
  return (
    <div>
      <button type="button" onClick={open} className={fileButton}>
        <DocumentTextIcon className="size-5" aria-hidden="true" />
        <span className="max-w-56 truncate">{filename || 'Abrir PDF'}</span>
      </button>
      {error && (
        <p role="alert" className="mt-1 text-sm text-red-700">
          {error}
        </p>
      )}
    </div>
  )
}

function Media({ message, label }) {
  const kind = message.media_kind
  if (!kind) return null
  if (!message.attachment_id) {
    return <p className="text-sm text-slate-500 italic">Archivo no incluido en la exportación.</p>
  }
  if (kind === 'audio') {
    return (
      <AudioMessage
        attachmentId={message.attachment_id}
        transcript={message.transcript}
        label={`Nota de voz: ${label}`}
      />
    )
  }
  if (kind === 'image') return <PhotoPreview attachmentId={message.attachment_id} label={label} />
  if (kind === 'pdf') {
    return <PdfLink attachmentId={message.attachment_id} filename={message.filename} />
  }
  return (
    <p className="flex items-center gap-2 text-sm text-slate-500">
      <FilmIcon className="size-5" aria-hidden="true" />
      {kind === 'video' ? 'Video (no se muestra aquí)' : 'Archivo'}
    </p>
  )
}

/**
 * The WhatsApp messages a suggestion is based on: sender and time, text,
 * voice note with its transcript, photo (on demand) or PDF link, and the
 * automatic description of an image/PDF.
 */
export default function SourceMessages({ messages }) {
  if (!messages.length) return null
  return (
    <ul className="space-y-3">
      {messages.map((message) => {
        const when = formatSentAt(message.sent_at)
        const label = `${message.sender ?? 'Mensaje'}, ${when}`
        const description =
          message.classification?.status === 'done' ? message.classification.description : null
        return (
          <li key={message.id} className="rounded-2xl bg-slate-50 p-3">
            <p className="text-sm text-slate-500">
              <span className="font-medium text-slate-700">{message.sender ?? 'Sin nombre'}</span>
              {' · '}
              {when}
              {message.forwarded && ' · Reenviado'}
            </p>
            {message.text && (
              <p className="mt-1 break-words whitespace-pre-wrap text-slate-900">{message.text}</p>
            )}
            {message.media_kind && (
              <div className="mt-2">
                <Media message={message} label={label} />
              </div>
            )}
            {description && <p className="mt-1 text-sm text-slate-600">{description}</p>}
          </li>
        )
      })}
    </ul>
  )
}
