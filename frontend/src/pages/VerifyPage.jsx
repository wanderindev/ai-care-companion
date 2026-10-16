import { useEffect, useRef, useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import AuthScreen from '../components/auth/AuthScreen.jsx'
import { readTokenFromHash, useVerify } from '../lib/auth.js'

/**
 * Landing page of the emailed link (`/acceso#token=…`). The token is in the
 * fragment so it never reaches a server log, and only this POST consumes it
 * (link scanners that prefetch the URL just load the SPA).
 */
export default function VerifyPage() {
  const [token] = useState(() => readTokenFromHash())
  const started = useRef(false)
  const verify = useVerify()
  const { mutate } = verify

  useEffect(() => {
    if (started.current || !token) return
    started.current = true
    // Drop the token from the address bar and history before using it.
    window.history.replaceState(null, '', window.location.pathname)
    mutate(token)
  }, [token, mutate])

  if (verify.isSuccess) return <Navigate to="/bitacora" replace />

  const failed = !token || verify.isError
  return (
    <AuthScreen>
      {failed ? (
        <div className="text-center">
          <h1 className="text-xl font-semibold">Enlace no válido</h1>
          <p className="mt-2 text-slate-600">
            {verify.error?.status === 400 || !token
              ? 'El enlace no es válido o ya venció. Pide uno nuevo.'
              : 'No se pudo conectar. Pide un enlace nuevo e inténtalo otra vez.'}
          </p>
          <Link
            to="/entrar"
            replace
            className="mt-6 inline-flex min-h-12 w-full items-center justify-center rounded-full bg-brand-700 font-medium text-white"
          >
            Pedir otro enlace
          </Link>
        </div>
      ) : (
        <p className="text-center text-slate-600" role="status">
          Entrando…
        </p>
      )}
    </AuthScreen>
  )
}
