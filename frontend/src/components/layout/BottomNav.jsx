import { NavLink } from 'react-router-dom'
import {
  BeakerIcon,
  BookOpenIcon,
  CalendarDaysIcon,
  ChatBubbleLeftRightIcon,
  ClipboardDocumentListIcon,
} from '@heroicons/react/24/outline'

const ITEMS = [
  { to: '/bitacora', label: 'Bitácora', Icon: BookOpenIcon },
  { to: '/calendario', label: 'Calendario', Icon: CalendarDaysIcon },
  { to: '/medicamentos', label: 'Medicamentos', Icon: ClipboardDocumentListIcon },
  { to: '/labs', label: 'Labs', Icon: BeakerIcon },
  { to: '/chat', label: 'Chat', Icon: ChatBubbleLeftRightIcon },
]

export default function BottomNav() {
  return (
    <nav
      aria-label="Secciones"
      className="fixed inset-x-0 bottom-0 z-20 border-t border-slate-200 bg-white pb-[env(safe-area-inset-bottom)]"
    >
      <ul className="mx-auto grid max-w-3xl grid-cols-5">
        {ITEMS.map(({ to, label, Icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              className={({ isActive }) =>
                `flex min-h-16 flex-col items-center justify-center gap-1 px-1 text-[11px] font-medium ${
                  isActive ? 'text-brand-700' : 'text-slate-500 active:text-slate-700'
                }`
              }
            >
              <Icon className="size-6" aria-hidden="true" />
              <span className="truncate">{label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
