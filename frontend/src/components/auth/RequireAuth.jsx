import { Navigate } from 'react-router-dom'
import { useMe } from '../../lib/auth.js'

/** Renders its children only for a signed-in user; otherwise sends to /entrar. */
export default function RequireAuth({ children }) {
  const me = useMe()

  if (me.isPending) {
    return (
      <p className="py-24 text-center text-slate-500" role="status">
        Cargando…
      </p>
    )
  }
  if (me.isError) {
    if (me.error.status === 401) return <Navigate to="/entrar" replace />
    return (
      <div className="py-24 text-center">
        <p className="text-slate-600">No se pudo conectar con el servidor.</p>
        <button
          type="button"
          onClick={() => me.refetch()}
          className="mt-4 min-h-12 rounded-full bg-brand-700 px-6 font-medium text-white"
        >
          Reintentar
        </button>
      </div>
    )
  }
  return children
}
