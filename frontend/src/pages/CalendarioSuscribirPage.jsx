import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowPathIcon,
  CalendarDaysIcon,
  CheckIcon,
  ChevronLeftIcon,
  ClipboardDocumentIcon,
  EnvelopeIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline'
import {
  useCalendarFeed,
  useGenerateCalendarFeed,
  useRevokeCalendarFeed,
} from '../lib/calendarFeed.js'
import { useUpdateUserSettings, useUserSettings } from '../lib/userSettings.js'
import { errorMessage } from '../lib/bitacora.js'
import { ErrorState } from '../components/bitacora/ListStates.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'

const DATE_FMT = new Intl.DateTimeFormat('es-PA', {
  day: 'numeric',
  month: 'long',
  year: 'numeric',
  timeZone: 'America/Panama',
})

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
        {copied ? 'Copiado' : 'Copiar enlace'}
      </button>
      <span role="status" className="sr-only">
        {copied ? 'Enlace copiado' : ''}
      </span>
    </div>
  )
}

/**
 * «Recibir invitaciones por correo» (DESIGN §6.3): opt-in email with an
 * .ics invitation on every appointment created, changed or cancelled.
 */
function EmailInvites() {
  const settings = useUserSettings()
  const update = useUpdateUserSettings()
  const enabled = Boolean(settings.data?.email_invites)

  return (
    <section
      aria-labelledby="invitaciones"
      className="mt-6 rounded-3xl border border-slate-200 bg-white p-4 shadow-sm"
    >
      {settings.isPending ? (
        <div className="h-20 animate-pulse rounded-2xl bg-slate-100" aria-label="Cargando" />
      ) : settings.isError ? (
        <ErrorState
          message={errorMessage(settings.error, 'No se pudo cargar tu configuración.')}
          onRetry={() => settings.refetch()}
        />
      ) : (
        <>
          <div className="flex items-start gap-3">
            <EnvelopeIcon className="mt-0.5 size-6 shrink-0 text-brand-700" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <h2 id="invitaciones" className="font-semibold text-slate-900">
                Recibir invitaciones por correo
              </h2>
              <p className="mt-1 text-sm text-slate-600">
                Te enviamos un correo con la invitación cada vez que se crea, cambia o cancela una
                cita, para que tu calendario se actualice al instante (el enlace de arriba puede
                tardar horas). Llega a <span className="break-all">{settings.data.email}</span> y
                nunca incluye las notas.
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              aria-labelledby="invitaciones"
              disabled={update.isPending}
              onClick={() => update.mutate({ email_invites: !enabled })}
              className="-my-2 -mr-1 flex h-12 w-16 shrink-0 items-center justify-center rounded-full disabled:opacity-60"
            >
              <span
                aria-hidden="true"
                className={`flex h-8 w-14 items-center rounded-full p-1 transition-colors ${
                  enabled ? 'bg-brand-700' : 'bg-slate-300'
                }`}
              >
                <span
                  className={`size-6 rounded-full bg-white shadow transition-transform ${
                    enabled ? 'translate-x-6' : 'translate-x-0'
                  }`}
                />
              </span>
            </button>
          </div>
          {update.isError && (
            <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
              {errorMessage(update.error, 'No se pudo guardar el cambio.')}
            </p>
          )}
        </>
      )}
    </section>
  )
}

function Steps({ title, children }) {
  return (
    <details className="group rounded-2xl border border-slate-200 bg-white shadow-sm">
      <summary className="flex min-h-14 cursor-pointer items-center px-4 font-semibold text-slate-900">
        {title}
      </summary>
      <div className="space-y-2 px-4 pb-4 text-slate-700">{children}</div>
    </details>
  )
}

function Instructions({ webcalUrl }) {
  return (
    <section aria-labelledby="instrucciones" className="mt-6">
      <h2 id="instrucciones" className="mb-3 text-lg font-semibold">
        Cómo agregarlo
      </h2>
      <div className="space-y-3">
        <Steps title="Google Calendar">
          <p>
            Hay que hacerlo desde la <strong>web</strong> (calendar.google.com, en una computadora o
            en el navegador del teléfono en modo «sitio para computadora»): la app de Google
            Calendar no permite suscribirse a un enlace.
          </p>
          <ol className="list-decimal space-y-1 pl-5">
            <li>Abre calendar.google.com.</li>
            <li>
              En el panel izquierdo, junto a «Otros calendarios», toca <strong>+</strong> y elige{' '}
              <strong>Desde URL</strong>.
            </li>
            <li>
              Pega el enlace y toca <strong>Añadir calendario</strong>.
            </li>
            <li>Ya aparece en la app del teléfono con la misma cuenta.</li>
          </ol>
          <p className="text-sm text-slate-500">
            Google actualiza los calendarios suscritos cada pocas horas: un cambio puede tardar en
            verse.
          </p>
        </Steps>
        <Steps title="Apple Calendar (iPhone y Mac)">
          <p className="font-medium">iPhone o iPad</p>
          <ol className="list-decimal space-y-1 pl-5">
            <li>
              Abre <strong>Ajustes</strong> → <strong>Calendario</strong> →{' '}
              <strong>Cuentas</strong> → <strong>Añadir cuenta</strong> → <strong>Otra</strong>.
            </li>
            <li>
              Toca <strong>Añadir calendario suscrito</strong>, pega el enlace y toca{' '}
              <strong>Siguiente</strong> y luego <strong>Guardar</strong>.
            </li>
          </ol>
          {webcalUrl && (
            <p>
              O, en este mismo iPhone,{' '}
              <a href={webcalUrl} className="font-medium text-brand-700 underline">
                toca aquí para suscribirte
              </a>
              .
            </p>
          )}
          <p className="font-medium">Mac</p>
          <ol className="list-decimal space-y-1 pl-5">
            <li>
              En la app Calendario: <strong>Archivo</strong> → <strong>Nueva suscripción a
              calendario</strong>.
            </li>
            <li>Pega el enlace, toca «Suscribirse» y elige cada cuánto actualizar (cada hora).</li>
          </ol>
        </Steps>
        <Steps title="Outlook">
          <ol className="list-decimal space-y-1 pl-5">
            <li>Abre outlook.com (u Outlook en la web) y ve al calendario.</li>
            <li>
              Toca <strong>Agregar calendario</strong> → <strong>Suscribirse desde la web</strong>.
            </li>
            <li>
              Pega el enlace, ponle un nombre (por ejemplo «Citas») y toca <strong>Importar</strong>.
            </li>
          </ol>
          <p className="text-sm text-slate-500">Outlook también tarda unas horas en actualizarlo.</p>
        </Steps>
        <Steps title="Zoho Calendar">
          <ol className="list-decimal space-y-1 pl-5">
            <li>Abre calendar.zoho.com.</li>
            <li>
              En el panel izquierdo, junto a «Calendarios», toca <strong>+</strong> →{' '}
              <strong>Suscribir</strong> (o «Calendarios suscritos» → <strong>Suscribir</strong>).
            </li>
            <li>
              Elige <strong>URL</strong>, pega el enlace y toca <strong>Suscribir</strong>.
            </li>
          </ol>
        </Steps>
      </div>
    </section>
  )
}

/**
 * «Calendario en tu teléfono» (DESIGN §6.3): a secret ICS link with both
 * patients' appointments. Only the link's hash is stored, so the link is
 * shown once, right after «Generar enlace»; afterwards the screen says since
 * when it is active and offers «Generar uno nuevo» (the old one stops
 * working) and «Desactivar».
 */
export default function CalendarioSuscribirPage() {
  const feed = useCalendarFeed()
  const generate = useGenerateCalendarFeed()
  const revoke = useRevokeCalendarFeed()
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

  const active = feed.data?.active
  const busy = generate.isPending || revoke.isPending

  return (
    <section className="mx-auto max-w-2xl pb-16 lg:pb-0">
      <Link
        to="/calendario"
        className="mb-2 -ml-2 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 font-medium text-brand-700 active:bg-brand-50"
      >
        <ChevronLeftIcon className="size-5" aria-hidden="true" />
        Calendario
      </Link>
      <h1 className="text-2xl font-semibold">Calendario en tu teléfono</h1>
      <p className="mt-2 text-slate-600">
        Suscríbete a las citas de Papá y Mamá desde Google Calendar, Apple Calendar, Outlook o
        Zoho. Las citas se actualizan solas; el enlace es personal y secreto: no lo compartas.
      </p>
      <p className="mt-2 text-sm text-slate-500">
        Incluye las citas de los últimos 6 meses y todas las próximas, con médico, motivo y lugar
        (nunca las notas).
      </p>

      <div className="mt-5 rounded-3xl border border-slate-200 bg-white p-4 shadow-sm">
        {feed.isPending ? (
          <div className="h-24 animate-pulse rounded-2xl bg-slate-100" aria-label="Cargando" />
        ) : feed.isError ? (
          <ErrorState
            message={errorMessage(feed.error, 'No se pudo cargar el estado del enlace.')}
            onRetry={() => feed.refetch()}
          />
        ) : fresh ? (
          <div className="space-y-3">
            <p className="flex items-center gap-2 font-semibold text-brand-800">
              <CheckIcon className="size-5" aria-hidden="true" />
              Enlace listo
            </p>
            <CopyField label="Tu enlace del calendario" value={fresh.url} />
            <p className="flex gap-2 rounded-2xl bg-amber-50 px-3 py-2 text-sm text-amber-900">
              <ExclamationTriangleIcon className="size-5 shrink-0" aria-hidden="true" />
              Cópialo ahora: por seguridad no se puede volver a mostrar. Si lo pierdes, genera uno
              nuevo.
            </p>
          </div>
        ) : active ? (
          <div className="space-y-3">
            <p className="flex items-center gap-2 font-semibold text-slate-900">
              <CalendarDaysIcon className="size-6 text-brand-700" aria-hidden="true" />
              Enlace activo desde el {DATE_FMT.format(new Date(feed.data.created_at))}
            </p>
            <p className="text-sm text-slate-600">
              Por seguridad, el enlace solo se muestra al generarlo. Si necesitas agregarlo en otro
              calendario, genera uno nuevo (el anterior dejará de funcionar).
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <button
                type="button"
                onClick={() => setConfirm('regenerate')}
                disabled={busy}
                className="flex min-h-12 items-center justify-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
              >
                <ArrowPathIcon className="size-5" aria-hidden="true" />
                Generar uno nuevo
              </button>
              <button
                type="button"
                onClick={() => setConfirm('revoke')}
                disabled={busy}
                className="min-h-12 rounded-2xl px-4 font-medium text-red-700 ring-1 ring-red-200 active:bg-red-50 disabled:opacity-60"
              >
                Desactivar
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-slate-700">Todavía no tienes un enlace.</p>
            <button
              type="button"
              onClick={doGenerate}
              disabled={busy}
              className="flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800 disabled:opacity-60 sm:w-auto"
            >
              <CalendarDaysIcon className="size-5" aria-hidden="true" />
              {generate.isPending ? 'Generando…' : 'Generar enlace'}
            </button>
          </div>
        )}
        {!confirm && (generate.isError || revoke.isError) && (
          <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
            {errorMessage(generate.error ?? revoke.error)}
          </p>
        )}
      </div>

      <EmailInvites />

      <Instructions webcalUrl={fresh?.webcal_url} />

      {confirm === 'regenerate' && (
        <ConfirmDialog
          title="¿Generar un enlace nuevo?"
          message="El enlace actual dejará de funcionar: los calendarios suscritos con él ya no se actualizarán y tendrás que agregarlo de nuevo."
          confirmLabel="Generar nuevo"
          busy={generate.isPending}
          error={generate.isError ? errorMessage(generate.error) : ''}
          onConfirm={doGenerate}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm === 'revoke' && (
        <ConfirmDialog
          title="¿Desactivar el enlace?"
          message="Los calendarios suscritos dejarán de recibir las citas."
          confirmLabel="Desactivar"
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
