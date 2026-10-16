import { useId } from 'react'
import { createPortal } from 'react-dom'
import { useModal } from '../../lib/useModal.js'

/**
 * In-app confirmation (never window.confirm). Mount it only while open; focus
 * starts on the safe "cancel" button.
 */
export default function ConfirmDialog({
  title,
  message,
  confirmLabel,
  cancelLabel = 'Cancelar',
  danger = false,
  busy = false,
  error = '',
  onConfirm,
  onCancel,
}) {
  const ref = useModal(busy ? undefined : onCancel)
  const titleId = useId()
  const messageId = useId()

  return createPortal(
    <div
      ref={ref}
      tabIndex={-1}
      className="fixed inset-0 z-50 flex items-end justify-center p-4 outline-none sm:items-center"
    >
      <div aria-hidden="true" className="sheet-backdrop absolute inset-0 bg-slate-900/50" />
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
        className="dialog-panel relative w-full max-w-sm rounded-3xl bg-white p-5 shadow-xl"
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {title}
        </h2>
        <div id={messageId} className="mt-2 text-slate-600">
          {message}
        </div>
        {error && (
          <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
            {error}
          </p>
        )}
        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            data-autofocus
            onClick={onCancel}
            disabled={busy}
            className="min-h-12 rounded-2xl px-4 font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-60"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={`min-h-12 rounded-2xl px-4 font-semibold text-white disabled:opacity-60 ${
              danger ? 'bg-red-700 active:bg-red-800' : 'bg-brand-700 active:bg-brand-800'
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
