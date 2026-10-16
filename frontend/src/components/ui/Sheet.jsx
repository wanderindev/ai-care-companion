import { useId } from 'react'
import { createPortal } from 'react-dom'
import { XMarkIcon } from '@heroicons/react/24/outline'
import { useModal } from '../../lib/useModal.js'

/**
 * Bottom sheet on phones, right drawer from `lg` up (docs/DESIGN.md §3).
 * Mount it only while open. Escape, the close button and a tap on the
 * backdrop call `onClose`; focus is trapped inside and restored on close.
 */
export default function Sheet({ title, onClose, children, footer, wide = false }) {
  const ref = useModal(onClose)
  const titleId = useId()

  return createPortal(
    <div ref={ref} tabIndex={-1} className="fixed inset-0 z-40 outline-none">
      <div
        aria-hidden="true"
        onClick={onClose}
        className="sheet-backdrop absolute inset-0 bg-slate-900/40"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-autofocus
        className={`sheet-panel absolute outline-none inset-x-0 bottom-0 flex max-h-[92dvh] flex-col rounded-t-3xl bg-white shadow-xl lg:inset-y-0 lg:right-0 lg:left-auto lg:max-h-none lg:w-full lg:rounded-none lg:rounded-l-2xl ${
          wide ? 'lg:max-w-xl' : 'lg:max-w-md'
        }`}
      >
        <div className="mx-auto mt-2 h-1.5 w-10 shrink-0 rounded-full bg-slate-300 lg:hidden" />
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-100 py-1 pr-2 pl-4">
          <h2 id={titleId} className="text-lg font-semibold">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Cerrar"
            className="flex size-11 items-center justify-center rounded-full text-slate-500 active:bg-slate-100"
          >
            <XMarkIcon className="size-6" aria-hidden="true" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4">
          {children}
        </div>
        {footer && (
          <div className="shrink-0 border-t border-slate-100 px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
