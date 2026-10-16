import { BookOpenIcon, MagnifyingGlassIcon } from '@heroicons/react/24/outline'

export function CardSkeletons({ count = 4 }) {
  return (
    <ul aria-hidden="true" className="space-y-3">
      {Array.from({ length: count }, (_, i) => (
        <li key={i} className="animate-pulse rounded-2xl border border-slate-200 bg-white p-4">
          <div className="h-4 w-28 rounded bg-slate-200" />
          <div className="mt-3 h-5 w-40 rounded bg-slate-200" />
          <div className="mt-2 h-4 w-full rounded bg-slate-100" />
          <div className="mt-1 h-4 w-3/4 rounded bg-slate-100" />
        </li>
      ))}
    </ul>
  )
}

export function TableSkeleton({ rows = 6 }) {
  return (
    <div aria-hidden="true" className="animate-pulse rounded-2xl border border-slate-200 bg-white">
      <div className="h-11 border-b border-slate-200 bg-slate-50" />
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex gap-4 border-b border-slate-100 px-4 py-4 last:border-0">
          <div className="h-4 w-24 rounded bg-slate-200" />
          <div className="h-4 w-40 rounded bg-slate-200" />
          <div className="h-4 w-20 rounded bg-slate-100" />
          <div className="h-4 flex-1 rounded bg-slate-100" />
        </div>
      ))}
    </div>
  )
}

/** No entries at all, or none matching the filters. */
export function EmptyState({ filtered, onClear, onCreate }) {
  const Icon = filtered ? MagnifyingGlassIcon : BookOpenIcon
  return (
    <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
      <Icon className="mx-auto size-10 text-slate-400" aria-hidden="true" />
      <p className="mt-3 font-medium text-slate-800">
        {filtered ? 'Ninguna entrada coincide con los filtros.' : 'Todavía no hay entradas.'}
      </p>
      <p className="mt-1 text-sm text-slate-500">
        {filtered
          ? 'Prueba con otras palabras o quita algún filtro.'
          : 'Anota aquí cada consulta, visita u observación.'}
      </p>
      <button
        type="button"
        onClick={filtered ? onClear : onCreate}
        className="mt-5 min-h-12 rounded-2xl bg-brand-700 px-5 font-semibold text-white active:bg-brand-800"
      >
        {filtered ? 'Quitar filtros' : 'Agregar la primera entrada'}
      </button>
    </div>
  )
}

export function ErrorState({ message, onRetry }) {
  return (
    <div role="alert" className="rounded-2xl border border-red-200 bg-red-50 px-6 py-8 text-center">
      <p className="font-medium text-red-900">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 min-h-11 rounded-2xl bg-white px-4 font-medium text-red-900 ring-1 ring-red-300 active:bg-red-100"
        >
          Reintentar
        </button>
      )}
    </div>
  )
}
