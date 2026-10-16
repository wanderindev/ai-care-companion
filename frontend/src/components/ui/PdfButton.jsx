import { useEffect, useRef, useState } from 'react'
import { ArrowTopRightOnSquareIcon, PrinterIcon, ShareIcon } from '@heroicons/react/24/outline'
import { XMarkIcon } from '@heroicons/react/20/solid'
import { errorMessage } from '../../lib/bitacora.js'

const buttonClass =
  'flex min-h-12 shrink-0 items-center gap-2 rounded-2xl px-4 font-semibold text-brand-700 ring-1 ring-slate-300 active:bg-brand-50 disabled:opacity-60'

function canShareFile(file) {
  try {
    return Boolean(navigator.canShare?.({ files: [file] }))
  } catch {
    return false
  }
}

/**
 * A server-made PDF behind a button (medication list, pre-visit summary,
 * emergency card).
 *
 * Two taps on purpose. The PDF needs the session cookie, so it is fetched
 * here (`fetchFile({signal})` → `File`), not linked: an iPhone home-screen
 * app opens links in a separate browser view without the session. Once it is
 * ready the second tap opens it («Abrir PDF», a blob URL: the browser's
 * viewer prints and saves) or hands it to the phone's share sheet
 * («Compartir o imprimir»: Imprimir, Guardar en Archivos, WhatsApp…). Sharing
 * needs a fresh tap: browsers refuse it after the wait for the download.
 * Changing `resetKey` (e.g. another patient) drops a ready PDF.
 */
export default function PdfButton({
  fetchFile,
  label,
  ariaLabel,
  shareTitle,
  icon: Icon = PrinterIcon,
  resetKey,
  disabled = false,
}) {
  const [state, setState] = useState({ status: 'idle' }) // idle | loading | ready | error
  const controller = useRef(null)
  const [seenKey, setSeenKey] = useState(resetKey)

  if (seenKey !== resetKey) {
    setSeenKey(resetKey)
    setState({ status: 'idle' })
  }

  // Free the blob URL when it is replaced or the button goes away.
  const url = state.status === 'ready' ? state.url : null
  useEffect(() => {
    if (!url) return undefined
    return () => URL.revokeObjectURL(url)
  }, [url])
  useEffect(() => () => controller.current?.abort(), [resetKey])

  async function prepare() {
    controller.current?.abort()
    controller.current = new AbortController()
    setState({ status: 'loading' })
    try {
      const file = await fetchFile({ signal: controller.current.signal })
      setState({
        status: 'ready',
        file,
        url: URL.createObjectURL(file),
        shareable: canShareFile(file),
      })
    } catch (error) {
      if (error?.name === 'AbortError') return
      setState({ status: 'error', error })
    }
  }

  async function share() {
    try {
      await navigator.share({ files: [state.file], title: shareTitle })
    } catch {
      // Cancelled or refused: «Abrir PDF» is still there.
    }
  }

  if (state.status === 'ready') {
    return (
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="PDF listo">
        <a
          href={state.url}
          target="_blank"
          rel="noopener"
          className="flex min-h-12 shrink-0 items-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800"
        >
          <ArrowTopRightOnSquareIcon className="size-5" aria-hidden="true" />
          Abrir PDF
        </a>
        {state.shareable && (
          <button type="button" onClick={share} className={buttonClass}>
            <ShareIcon className="size-5" aria-hidden="true" />
            Compartir o imprimir
          </button>
        )}
        <button
          type="button"
          onClick={() => setState({ status: 'idle' })}
          aria-label="Cerrar PDF"
          className="flex size-12 shrink-0 items-center justify-center rounded-2xl text-slate-600 active:bg-slate-100"
        >
          <XMarkIcon className="size-5" aria-hidden="true" />
        </button>
      </div>
    )
  }

  const loading = state.status === 'loading'
  return (
    <div className="flex flex-col items-start gap-1">
      <button
        type="button"
        onClick={prepare}
        disabled={loading || disabled}
        aria-label={loading ? undefined : ariaLabel}
        className={buttonClass}
      >
        <Icon className="size-5" aria-hidden="true" />
        {loading ? 'Preparando PDF…' : label}
      </button>
      {state.status === 'error' && (
        <p role="alert" className="text-sm text-red-700">
          {errorMessage(state.error, 'No se pudo preparar el PDF. Inténtalo de nuevo.')}
        </p>
      )}
    </div>
  )
}
