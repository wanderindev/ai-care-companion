import { useState } from 'react'
import { EnvelopeIcon } from '@heroicons/react/24/outline'
import AuthScreen from '../components/auth/AuthScreen.jsx'
import { useRequestLink } from '../lib/auth.js'

export default function LoginPage() {
  const [email, setEmail] = useState('')
  const requestLink = useRequestLink()

  function handleSubmit(event) {
    event.preventDefault()
    requestLink.mutate(email.trim())
  }

  if (requestLink.isSuccess) {
    return (
      <AuthScreen>
        <div className="text-center">
          <EnvelopeIcon className="mx-auto size-12 text-brand-700" aria-hidden="true" />
          <h1 className="mt-3 text-xl font-semibold">Revisa tu correo</h1>
          <p className="mt-2 text-slate-600">
            Si <strong className="break-all">{email.trim()}</strong> está autorizado, te enviamos
            un enlace para entrar. Vence en 15 minutos.
          </p>
          <button
            type="button"
            onClick={() => requestLink.reset()}
            className="mt-6 min-h-12 w-full rounded-full border border-slate-300 font-medium text-slate-700 active:bg-slate-100"
          >
            Usar otro correo
          </button>
        </div>
      </AuthScreen>
    )
  }

  return (
    <AuthScreen>
      <h1 className="text-xl font-semibold">Entrar</h1>
      <p className="mt-1 text-slate-600">Te enviaremos un enlace a tu correo. Sin contraseñas.</p>
      <form onSubmit={handleSubmit} className="mt-6 space-y-4">
        <label className="block">
          <span className="text-sm font-medium text-slate-700">Correo electrónico</span>
          <input
            type="email"
            inputMode="email"
            autoComplete="email"
            autoCapitalize="none"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="tu@correo.com"
            className="mt-1 block min-h-12 w-full rounded-xl border border-slate-300 px-4 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none"
          />
        </label>
        {requestLink.isError && (
          <p role="alert" className="text-sm text-red-700">
            No se pudo enviar el enlace. Inténtalo de nuevo.
          </p>
        )}
        <button
          type="submit"
          disabled={requestLink.isPending}
          className="min-h-12 w-full rounded-full bg-brand-700 font-medium text-white active:bg-brand-800 disabled:opacity-60"
        >
          {requestLink.isPending ? 'Enviando…' : 'Enviarme el enlace'}
        </button>
      </form>
    </AuthScreen>
  )
}
