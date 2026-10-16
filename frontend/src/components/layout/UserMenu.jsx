import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  ArrowRightStartOnRectangleIcon,
  BeakerIcon,
  CalendarDaysIcon,
  ChatBubbleLeftRightIcon,
  ExclamationTriangleIcon,
  UserCircleIcon,
  UserGroupIcon,
} from '@heroicons/react/24/outline'
import { useLogout, useMe } from '../../lib/auth.js'

/**
 * Header avatar button: the signed-in user, "Tarjeta de emergencia",
 * "Médicos", "Calendario en tu teléfono" (the personal ICS feed), "Importar
 * WhatsApp"
 * and "Revisar exámenes" (admins only) and "Cerrar sesión".
 */
export default function UserMenu() {
  const { data: user } = useMe()
  const logout = useLogout()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    function onPointerDown(event) {
      if (ref.current && !ref.current.contains(event.target)) setOpen(false)
    }
    function onKey(event) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  function handleLogout() {
    logout.mutate(undefined, { onSettled: () => navigate('/entrar', { replace: true }) })
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-label="Cuenta"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex size-11 items-center justify-center rounded-full text-slate-600 active:bg-slate-100"
      >
        <UserCircleIcon className="size-7" aria-hidden="true" />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-30 mt-2 w-64 rounded-2xl border border-slate-200 bg-white p-2 shadow-lg"
        >
          {user && (
            <div className="px-3 py-2">
              <p className="truncate font-medium">{user.display_name}</p>
              <p className="truncate text-sm text-slate-500">{user.email}</p>
            </div>
          )}
          <Link
            to="/emergencia"
            role="menuitem"
            onClick={() => setOpen(false)}
            className="flex min-h-12 w-full items-center gap-3 rounded-xl px-3 font-medium text-slate-700 active:bg-slate-100"
          >
            <ExclamationTriangleIcon className="size-6" aria-hidden="true" />
            Tarjeta de emergencia
          </Link>
          <Link
            to="/medicos"
            role="menuitem"
            onClick={() => setOpen(false)}
            className="flex min-h-12 w-full items-center gap-3 rounded-xl px-3 font-medium text-slate-700 active:bg-slate-100"
          >
            <UserGroupIcon className="size-6" aria-hidden="true" />
            Médicos
          </Link>
          <Link
            to="/calendario/suscribir"
            role="menuitem"
            onClick={() => setOpen(false)}
            className="flex min-h-12 w-full items-center gap-3 rounded-xl px-3 font-medium text-slate-700 active:bg-slate-100"
          >
            <CalendarDaysIcon className="size-6" aria-hidden="true" />
            Calendario en tu teléfono
          </Link>
          {user?.can_import && (
            <Link
              to="/importar-whatsapp"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="flex min-h-12 w-full items-center gap-3 rounded-xl px-3 font-medium text-slate-700 active:bg-slate-100"
            >
              <ChatBubbleLeftRightIcon className="size-6" aria-hidden="true" />
              Importar WhatsApp
            </Link>
          )}
          {user?.is_admin && (
            <Link
              to="/labs/revisar-examenes"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="flex min-h-12 w-full items-center gap-3 rounded-xl px-3 font-medium text-slate-700 active:bg-slate-100"
            >
              <BeakerIcon className="size-6" aria-hidden="true" />
              Revisar exámenes
            </Link>
          )}
          <button
            type="button"
            role="menuitem"
            onClick={handleLogout}
            disabled={logout.isPending}
            className="flex min-h-12 w-full items-center gap-3 rounded-xl px-3 text-left font-medium text-slate-700 active:bg-slate-100 disabled:opacity-60"
          >
            <ArrowRightStartOnRectangleIcon className="size-6" aria-hidden="true" />
            Cerrar sesión
          </button>
        </div>
      )}
    </div>
  )
}
