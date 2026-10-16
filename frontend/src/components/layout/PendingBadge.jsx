import { NavLink } from 'react-router-dom'
import { InboxIcon } from '@heroicons/react/24/outline'
import { usePendingCount } from '../../lib/suggestions.js'

/**
 * Header link to «Pendientes» with the number of suggestions waiting
 * (polled every minute and when the app regains focus).
 */
export default function PendingBadge() {
  const { data: count = 0 } = usePendingCount()
  const label = count > 0 ? `Pendientes (${count})` : 'Pendientes'
  return (
    <NavLink
      to="/pendientes"
      aria-label={label}
      title={label}
      className={({ isActive }) =>
        `relative flex size-11 items-center justify-center rounded-full active:bg-slate-100 ${
          isActive ? 'text-brand-700' : 'text-slate-600'
        }`
      }
    >
      <InboxIcon className="size-7" aria-hidden="true" />
      {count > 0 && (
        <span
          aria-hidden="true"
          className="absolute top-0.5 right-0 min-w-5 rounded-full bg-red-600 px-1 text-center text-xs leading-5 font-semibold text-white"
        >
          {count > 99 ? '99+' : count}
        </span>
      )}
    </NavLink>
  )
}
