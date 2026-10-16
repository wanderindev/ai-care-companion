import { useEffect, useRef, useState } from 'react'
import {
  ArrowPathIcon,
  CheckIcon,
  ClipboardDocumentIcon,
  ExclamationTriangleIcon,
  KeyIcon,
} from '@heroicons/react/24/outline'
import {
  useExtensionToken,
  useGenerateExtensionToken,
  useRevokeExtensionToken,
} from '../../lib/extensionToken.js'
import { errorMessage } from '../../lib/bitacora.js'
import { formatDateTime } from '../../lib/whatsappImports.js'
import { ErrorState } from '../bitacora/ListStates.jsx'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'

function CopyField({ label, value }) {
  const [copied, setCopied] = useState(false)
  const inputRef = useRef(null)

  useEffect(() => {
    if (!copied) return undefined
    const id = setTimeout(() => setCopied(false), 2500)
    return () => clearTimeout(id)
  }, [copied])

  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
    } catch {
      // No clipboard API (or permission denied): select it for a manual copy.
      inputRef.current?.select()
    }
  }

  return (
    <div>
      <label className="mb-1 block text-sm font-medium text-slate-600">
        {label}
        <input
          ref={inputRef}
          readOnly
          value={value}
          onFocus={(e) => e.target.select()}
          className="mt-1 block w-full rounded-xl border border-slate-300 bg-slate-50 px-3 py-3 font-mono text-sm text-slate-800"
        />
      </label>
      <button
        type="button"
        onClick={copy}
        className="mt-2 flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800 sm:w-auto"
      >
        {copied ? (
          <CheckIcon className="size-5" aria-hidden="true" />
        ) : (
          <ClipboardDocumentIcon className="size-5" aria-hidden="true" />
        )}
        {copied ? 'Copiado' : 'Copiar'}
      </button>
      <span role="status" className="sr-only">
        {copied ? `${label} copiado` : ''}
      </span>
    </div>
  )
}

/**
 * «Extensión de Chrome» (DESIGN §6.8, docs/extension-api.md): the admin's
 * secret key for the WhatsApp Web sync extension. Only its hash is stored, so
 * the key is shown once, right after «Generar clave»; afterwards the panel
 * says since when it is active and offers «Generar una nueva» (the old one
 * stops working) and «Revocar».
 */
export default function ExtensionPanel() {
  const status = useExtensionToken()
  const generate = useGenerateExtensionToken()
  const revoke = useRevokeExtensionToken()
  const [fresh, setFresh] = useState(null)
  const [confirm, setConfirm] = useState(null)

  function doGenerate() {
    generate.mutate(undefined, {
      onSuccess: (data) => {
        setFresh(data)
        setConfirm(null)
      },
    })
  }

  function doRevoke() {
    revoke.mutate(undefined, {
      onSuccess: () => {
        setFresh(null)
        setConfirm(null)
      },
    })
  }

  const busy = generate.isPending || revoke.isPending

  return (
    <section
      aria-labelledby="extension-chrome"
      className="rounded-3xl border border-slate-200 bg-white p-4 shadow-sm"
    >
      <h2 id="extension-chrome" className="text-lg font-semibold">
        Extensión de Chrome
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        La extensión sincroniza el grupo desde WhatsApp Web en tu computadora: sube los mensajes
        nuevos y luego sus fotos, audios y documentos. Para conectarla, pega en ella esta clave
        personal. Solo sirve para importar el chat; no la compartas.
      </p>

      <div className="mt-4">
        {status.isPending ? (
          <div className="h-20 animate-pulse rounded-2xl bg-slate-100" aria-label="Cargando" />
        ) : status.isError ? (
          <ErrorState
            message={errorMessage(status.error, 'No se pudo cargar el estado de la clave.')}
            onRetry={() => status.refetch()}
          />
        ) : fresh ? (
          <div className="space-y-3">
            <p className="flex items-center gap-2 font-semibold text-brand-800">
              <CheckIcon className="size-5" aria-hidden="true" />
              Clave lista
            </p>
            <CopyField label="Clave de la extensión" value={fresh.token} />
            <CopyField label="Dirección de la app" value={fresh.api_base} />
            <p className="flex gap-2 rounded-2xl bg-amber-50 px-3 py-2 text-sm text-amber-900">
              <ExclamationTriangleIcon className="size-5 shrink-0" aria-hidden="true" />
              Cópiala ahora en la extensión: por seguridad no se puede volver a mostrar. Si la
              pierdes, genera una nueva.
            </p>
          </div>
        ) : status.data?.active ? (
          <div className="space-y-3">
            <p className="flex items-center gap-2 font-semibold text-slate-900">
              <KeyIcon className="size-6 text-brand-700" aria-hidden="true" />
              Clave activa desde el {formatDateTime(status.data.created_at)}
            </p>
            <p className="text-sm text-slate-600">
              {status.data.last_used_at
                ? `Último uso: ${formatDateTime(status.data.last_used_at)}.`
                : 'Todavía no se ha usado.'}
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <button
                type="button"
                onClick={() => setConfirm('regenerate')}
                disabled={busy}
                className="flex min-h-12 items-center justify-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
              >
                <ArrowPathIcon className="size-5" aria-hidden="true" />
                Generar una nueva
              </button>
              <button
                type="button"
                onClick={() => setConfirm('revoke')}
                disabled={busy}
                className="min-h-12 rounded-2xl px-4 font-medium text-red-700 ring-1 ring-red-200 active:bg-red-50 disabled:opacity-60"
              >
                Revocar
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={doGenerate}
            disabled={busy}
            className="flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800 disabled:opacity-60 sm:w-auto"
          >
            <KeyIcon className="size-5" aria-hidden="true" />
            {generate.isPending ? 'Generando…' : 'Generar clave'}
          </button>
        )}
        {!confirm && (generate.isError || revoke.isError) && (
          <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
            {errorMessage(generate.error ?? revoke.error)}
          </p>
        )}
      </div>

      {confirm === 'regenerate' && (
        <ConfirmDialog
          title="¿Generar una clave nueva?"
          message="La clave actual dejará de funcionar: tendrás que pegar la nueva en la extensión."
          confirmLabel="Generar nueva"
          busy={generate.isPending}
          error={generate.isError ? errorMessage(generate.error) : ''}
          onConfirm={doGenerate}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm === 'revoke' && (
        <ConfirmDialog
          title="¿Revocar la clave?"
          message="La extensión dejará de poder subir mensajes hasta que pegues una clave nueva."
          confirmLabel="Revocar"
          danger
          busy={revoke.isPending}
          error={revoke.isError ? errorMessage(revoke.error) : ''}
          onConfirm={doRevoke}
          onCancel={() => setConfirm(null)}
        />
      )}
    </section>
  )
}
