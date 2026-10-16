import { useCallback, useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import { PaperAirplaneIcon } from '@heroicons/react/24/solid'

const MAX_LINES = 6
const LINE_PX = 24

/**
 * Height of the on-screen keyboard (iOS Safari, Android Chrome): the part of
 * the layout viewport the visual viewport no longer shows. 0 when closed or
 * when the browser resizes the page itself.
 */
function useKeyboardOffset() {
  const subscribe = useCallback((onChange) => {
    const vv = window.visualViewport
    if (!vv) return () => {}
    vv.addEventListener('resize', onChange)
    vv.addEventListener('scroll', onChange)
    return () => {
      vv.removeEventListener('resize', onChange)
      vv.removeEventListener('scroll', onChange)
    }
  }, [])
  return useSyncExternalStore(
    subscribe,
    () => {
      const vv = window.visualViewport
      if (!vv) return 0
      const offset = Math.round(window.innerHeight - vv.height - vv.offsetTop)
      // Small differences are browser chrome (URL bar) moving, not a keyboard.
      return offset > 80 ? offset : 0
    },
    () => 0,
  )
}

/**
 * Message box fixed above the bottom nav (or above the keyboard while it is
 * open). Enter adds a line (phones); Ctrl/⌘+Enter or the button sends.
 * `onHeight` reports the box's height so the page can leave room for it.
 */
export default function Composer({ value, onChange, onSend, disabled, hint, onHeight, autoFocus }) {
  const boxRef = useRef(null)
  const textRef = useRef(null)
  const keyboard = useKeyboardOffset()
  const canSend = !disabled && value.trim().length > 0

  useLayoutEffect(() => {
    const el = textRef.current
    if (!el) return
    el.style.height = 'auto'
    const max = MAX_LINES * LINE_PX + 20
    el.style.height = `${Math.min(el.scrollHeight, max)}px`
    el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden'
  }, [value])

  useEffect(() => {
    const el = boxRef.current
    if (!el || !onHeight || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => onHeight(el.offsetHeight))
    ro.observe(el)
    return () => ro.disconnect()
  }, [onHeight])

  function submit(event) {
    event?.preventDefault()
    if (canSend) onSend(value.trim())
  }

  function onKeyDown(event) {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) submit(event)
  }

  const style = keyboard
    ? { bottom: `${keyboard}px` }
    : { bottom: 'calc(4rem + 1px + env(safe-area-inset-bottom))' }

  return (
    <div
      ref={boxRef}
      style={style}
      className="fixed inset-x-0 z-30 border-t border-slate-200 bg-slate-50/95 backdrop-blur"
    >
      <form onSubmit={submit} className="mx-auto flex max-w-3xl items-end gap-2 px-4 pt-2">
        <label htmlFor="chat-composer" className="sr-only">
          Escribe tu pregunta
        </label>
        <textarea
          id="chat-composer"
          ref={textRef}
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          autoFocus={autoFocus}
          maxLength={8000}
          enterKeyHint="enter"
          placeholder="Pregunta sobre Papá o Mamá…"
          className="min-h-11 flex-1 resize-none rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-base leading-6 focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-100"
        />
        <button
          type="submit"
          disabled={!canSend}
          aria-label="Enviar"
          title="Enviar (Ctrl+Enter)"
          className="flex size-11 shrink-0 items-center justify-center rounded-full bg-brand-700 text-white active:bg-brand-800 disabled:bg-slate-300"
        >
          <PaperAirplaneIcon className="size-5" aria-hidden="true" />
        </button>
      </form>
      {hint && (
        <p className="mx-auto max-w-3xl px-4 pt-1.5 text-sm text-slate-600">{hint}</p>
      )}
      <p className="mx-auto max-w-3xl px-4 pt-1 pb-2 text-center text-[11px] leading-4 text-slate-500">
        Las respuestas se basan en los datos registrados. No reemplaza al médico.
      </p>
    </div>
  )
}
