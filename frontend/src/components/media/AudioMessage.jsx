import { useId, useState } from 'react'
import { ChevronDownIcon, PlayIcon } from '@heroicons/react/20/solid'
import { MusicalNoteIcon } from '@heroicons/react/24/outline'
import { getAttachmentUrl } from '../../lib/upload.js'

/**
 * A WhatsApp voice note: an audio player for an attachment plus its automatic
 * transcript (DESIGN §6.7; ticket #36, used by the Pendientes screens later).
 *
 * The presigned file URL is fetched only when the user presses play and kept
 * in component state while it is mounted, never cached elsewhere; if it has
 * expired (the browser reports an error) the next press fetches a new one.
 *
 * Props:
 * - `attachmentId` (number): the audio attachment.
 * - `transcript` (string | null): `whatsapp_messages.transcript`.
 * - `transcriptStatus`: 'done' | 'pending' | 'unavailable'. Defaults to
 *   'done' when a transcript is given, else 'pending'.
 * - `label` (string): accessible name, e.g. "Nota de voz de Ana, 3 abr 10:15".
 */
export default function AudioMessage({
  attachmentId,
  transcript = null,
  transcriptStatus,
  label = 'Nota de voz',
}) {
  const [url, setUrl] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const status = transcriptStatus ?? (transcript != null ? 'done' : 'pending')

  async function play() {
    setLoading(true)
    setError('')
    try {
      const data = await getAttachmentUrl(attachmentId)
      setUrl(data.url)
    } catch {
      setError('No se pudo cargar el audio. Inténtalo de nuevo.')
    } finally {
      setLoading(false)
    }
  }

  function onAudioError() {
    // Most likely an expired URL: drop it so the next press fetches a new one.
    setUrl(null)
    setError('No se pudo reproducir el audio. Inténtalo de nuevo.')
  }

  return (
    <section aria-label={label} className="rounded-2xl bg-white p-3 ring-1 ring-slate-200">
      <div className="flex items-center gap-2">
        <MusicalNoteIcon className="size-5 shrink-0 text-slate-400" aria-hidden="true" />
        {url ? (
          <audio
            controls
            autoPlay
            preload="auto"
            src={url}
            onError={onAudioError}
            className="w-full"
            aria-label={label}
          >
            Tu navegador no puede reproducir este audio.
          </audio>
        ) : (
          <button
            type="button"
            onClick={play}
            disabled={loading}
            className="flex min-h-11 items-center gap-1.5 rounded-xl px-3 text-sm font-medium text-brand-700 ring-1 ring-slate-200 active:bg-brand-50 disabled:opacity-60"
          >
            <PlayIcon className="size-5" aria-hidden="true" />
            {loading ? 'Cargando…' : 'Reproducir'}
            <span className="sr-only">: {label}</span>
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          {error}
        </p>
      )}
      <Transcript status={status} text={transcript} />
    </section>
  )
}

function Transcript({ status, text }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()

  if (status === 'pending') {
    return (
      <p className="mt-2 text-sm text-slate-500" aria-live="polite">
        Transcripción pendiente…
      </p>
    )
  }
  if (status === 'unavailable') {
    return <p className="mt-2 text-sm text-slate-500">Transcripción no disponible.</p>
  }
  const empty = !text || !text.trim()
  return (
    <div className="mt-2">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className="flex min-h-11 w-full items-center justify-between gap-2 rounded-xl px-1 text-left text-sm font-medium text-slate-700"
      >
        Transcripción automática
        <ChevronDownIcon
          className={`size-5 shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
          aria-hidden="true"
        />
      </button>
      <div id={panelId} hidden={!open}>
        {empty ? (
          <p className="px-1 text-sm text-slate-500">No se entendió ninguna palabra.</p>
        ) : (
          <p className="whitespace-pre-line px-1 text-base text-slate-800">{text}</p>
        )}
        <p className="mt-1 px-1 text-xs text-slate-500">
          Puede contener errores; escucha el audio si hay dudas.
        </p>
      </div>
    </div>
  )
}
