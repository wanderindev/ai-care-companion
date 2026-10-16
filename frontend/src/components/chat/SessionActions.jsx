import { useEffect, useRef, useState } from 'react'
import { ArchiveBoxArrowDownIcon, ArchiveBoxXMarkIcon, EllipsisVerticalIcon, TrashIcon } from '@heroicons/react/24/outline'
import { sessionActionError, useArchiveChatSession, useDeleteChatSession } from '../../lib/chat.js'
import { useIsDesktop } from '../../lib/useMediaQuery.js'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import Sheet from '../ui/Sheet.jsx'

function ActionButton({ icon: Icon, label, onClick, danger = false, disabled = false }) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      disabled={disabled}
      className={`flex min-h-12 w-full items-center gap-3 rounded-xl px-3 text-left font-medium active:bg-slate-100 disabled:opacity-60 ${
        danger ? 'text-red-700' : 'text-slate-700'
      }`}
    >
      <Icon className="size-6 shrink-0" aria-hidden="true" />
      {label}
    </button>
  )
}

/**
 * The «⋮» button of a conversation (list row or open session header) and its
 * menu: «Archivar» / «Restaurar» (any member) and «Eliminar» (only when the
 * server says `can_delete`: its creator or an admin), the latter after a
 * ConfirmDialog. A bottom sheet on phones, a small popover from `lg` up.
 * `open` / `onOpenChange` are controlled so a long press on the row can open
 * it too. `onDeleted()` runs after a delete (e.g. to leave the page).
 */
export default function SessionActions({ session, open, onOpenChange, onDeleted, label }) {
  const isDesktop = useIsDesktop()
  const archive = useArchiveChatSession()
  const remove = useDeleteChatSession()
  const [confirming, setConfirming] = useState(false)
  const wrapRef = useRef(null)
  const archived = Boolean(session.archived_at)
  const busy = archive.isPending || remove.isPending

  const close = () => {
    if (busy) return
    archive.reset()
    onOpenChange(false)
  }

  // Desktop popover: outside tap and Escape close it.
  useEffect(() => {
    if (!open || !isDesktop) return undefined
    function onPointerDown(event) {
      if (wrapRef.current && !wrapRef.current.contains(event.target)) onOpenChange(false)
    }
    function onKey(event) {
      if (event.key === 'Escape') onOpenChange(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, isDesktop, onOpenChange])

  function toggleArchive() {
    archive.mutate(
      { id: session.id, archived: !archived },
      { onSuccess: () => onOpenChange(false) },
    )
  }

  function askDelete() {
    archive.reset()
    onOpenChange(false)
    remove.reset()
    setConfirming(true)
  }

  function confirmDelete() {
    remove.mutate(session.id, {
      onSuccess: () => {
        setConfirming(false)
        onDeleted?.()
      },
    })
  }

  const actions = (
    <div role="menu" className="space-y-1">
      <ActionButton
        icon={archived ? ArchiveBoxXMarkIcon : ArchiveBoxArrowDownIcon}
        label={archive.isPending ? 'Guardando…' : archived ? 'Restaurar' : 'Archivar'}
        onClick={toggleArchive}
        disabled={busy}
      />
      {session.can_delete && (
        <ActionButton icon={TrashIcon} label="Eliminar" onClick={askDelete} danger disabled={busy} />
      )}
      {archive.isError && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {sessionActionError(archive.error)}
        </p>
      )}
    </div>
  )

  return (
    <div ref={wrapRef} className="relative shrink-0">
      <button
        type="button"
        aria-label={label ?? 'Opciones de la conversación'}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? close() : onOpenChange(true))}
        className="flex size-11 items-center justify-center rounded-full text-slate-500 active:bg-slate-100 lg:hover:bg-slate-100"
      >
        <EllipsisVerticalIcon className="size-6" aria-hidden="true" />
      </button>

      {open && isDesktop && (
        <div className="absolute right-0 z-30 mt-1 w-56 rounded-2xl border border-slate-200 bg-white p-2 shadow-lg">
          {actions}
        </div>
      )}
      {open && !isDesktop && (
        <Sheet title={session.title || 'Nueva conversación'} onClose={close}>
          <div className="p-3 pb-6">{actions}</div>
        </Sheet>
      )}

      {confirming && (
        <ConfirmDialog
          title="Eliminar conversación"
          message="Se eliminarán la conversación y sus mensajes para toda la familia. Las sugerencias que propuso siguen en «Pendientes». No se puede deshacer."
          confirmLabel={remove.isPending ? 'Eliminando…' : 'Eliminar'}
          danger
          busy={remove.isPending}
          error={remove.isError ? sessionActionError(remove.error) : ''}
          onConfirm={confirmDelete}
          onCancel={() => setConfirming(false)}
        />
      )}
    </div>
  )
}
