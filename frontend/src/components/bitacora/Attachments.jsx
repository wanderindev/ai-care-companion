import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowDownTrayIcon,
  ArrowTopRightOnSquareIcon,
  DocumentIcon,
  MusicalNoteIcon,
  PhotoIcon,
} from '@heroicons/react/24/outline'
import { getAttachmentUrl, openAttachment } from '../../lib/upload.js'
import { attachmentKind, errorMessage, formatSize } from '../../lib/bitacora.js'

export function AttachmentIcon({ mime, className }) {
  const kind = attachmentKind(mime)
  const Icon =
    kind === 'image' || kind === 'file-image'
      ? PhotoIcon
      : kind === 'audio'
        ? MusicalNoteIcon
        : DocumentIcon
  return <Icon className={className} aria-hidden="true" />
}

/**
 * Presigned URLs expire within minutes, so they are fetched right before use
 * and dropped soon after (never cached past half their lifetime).
 */
function useAttachmentUrl(id) {
  return useQuery({
    queryKey: ['attachment-url', id],
    queryFn: () => getAttachmentUrl(id),
    staleTime: (query) => ((query.state.data?.expires_in ?? 0) * 1000) / 2,
    gcTime: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
}

function FileActions({ attachment, canOpen, onError }) {
  const run = (options) => openAttachment(attachment.id, options).catch(onError)
  const buttonClass =
    'flex min-h-11 items-center gap-1.5 rounded-xl px-3 text-sm font-medium text-brand-700 ring-1 ring-slate-200 active:bg-brand-50'
  return (
    <div className="flex flex-wrap gap-2">
      {canOpen && (
        <button type="button" onClick={() => run()} className={buttonClass}>
          <ArrowTopRightOnSquareIcon className="size-4" aria-hidden="true" />
          Abrir
        </button>
      )}
      <button type="button" onClick={() => run({ download: true })} className={buttonClass}>
        <ArrowDownTrayIcon className="size-4" aria-hidden="true" />
        Descargar
      </button>
    </div>
  )
}

function FileRow({ attachment, note, canOpen, onError }) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-slate-200 bg-white p-3">
      <AttachmentIcon mime={attachment.mime} className="size-8 shrink-0 text-slate-400" />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{attachment.filename}</p>
        <p className="text-sm text-slate-500">
          {formatSize(attachment.size)}
          {note ? ` · ${note}` : ''}
        </p>
      </div>
      <FileActions attachment={attachment} canOpen={canOpen} onError={onError} />
    </div>
  )
}

function InlineImage({ attachment, onError }) {
  const url = useAttachmentUrl(attachment.id)
  const [broken, setBroken] = useState(false)

  if (broken || url.isError) {
    return (
      <FileRow
        attachment={attachment}
        note={url.isError ? 'No se pudo cargar la vista previa' : 'Sin vista previa'}
        canOpen={!url.isError}
        onError={onError}
      />
    )
  }
  return (
    <figure className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
      <button
        type="button"
        onClick={() => openAttachment(attachment.id).catch(onError)}
        className="block w-full bg-slate-100"
        aria-label={`Abrir ${attachment.filename} en grande`}
      >
        {url.data ? (
          <img
            src={url.data.url}
            alt={attachment.filename}
            loading="lazy"
            onError={() => setBroken(true)}
            className="mx-auto max-h-96 w-auto object-contain"
          />
        ) : (
          <div className="h-48 animate-pulse" />
        )}
      </button>
      <figcaption className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
        <span className="min-w-0 truncate text-slate-600">{attachment.filename}</span>
        <FileActions attachment={attachment} canOpen={false} onError={onError} />
      </figcaption>
    </figure>
  )
}

function AudioAttachment({ attachment, onError }) {
  const url = useAttachmentUrl(attachment.id)
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <p className="mb-2 flex items-center gap-2 truncate font-medium">
        <MusicalNoteIcon className="size-5 shrink-0 text-slate-400" aria-hidden="true" />
        {attachment.filename}
      </p>
      {url.data && (
        <audio controls preload="metadata" src={url.data.url} className="w-full">
          Tu navegador no puede reproducir este audio.
        </audio>
      )}
      {url.isError && <p className="text-sm text-red-700">No se pudo cargar el audio.</p>}
      <div className="mt-2">
        <FileActions attachment={attachment} canOpen={false} onError={onError} />
      </div>
    </div>
  )
}

/** Detail view previews: photos inline, audio playable, PDFs/HEIC as chips. */
export function AttachmentList({ attachments }) {
  const [error, setError] = useState('')
  const onError = (e) => setError(errorMessage(e, 'No se pudo abrir el archivo.'))

  return (
    <div className="space-y-3">
      {error && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}
      {attachments.map((a) => {
        const kind = attachmentKind(a.mime)
        if (kind === 'image') return <InlineImage key={a.id} attachment={a} onError={onError} />
        if (kind === 'audio') return <AudioAttachment key={a.id} attachment={a} onError={onError} />
        return (
          <FileRow
            key={a.id}
            attachment={a}
            note={kind === 'pdf' ? 'PDF' : kind === 'file-image' ? 'Foto HEIC' : ''}
            canOpen={kind === 'pdf'}
            onError={onError}
          />
        )
      })}
    </div>
  )
}
