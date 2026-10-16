import { useEffect, useRef } from 'react'

const LONG_PRESS_MS = 500
const MOVE_TOLERANCE_PX = 10

/**
 * Long press (touch) on a list row: calls `onLongPress` and swallows the
 * click that follows, so the row's link does not open. Spread the returned
 * handlers on the row's link.
 */
export function useLongPress(onLongPress) {
  const timer = useRef(null)
  const start = useRef(null)
  const fired = useRef(false)

  const cancel = () => {
    clearTimeout(timer.current)
    timer.current = null
  }
  useEffect(() => () => clearTimeout(timer.current), [])

  return {
    onPointerDown: (event) => {
      fired.current = false
      if (event.pointerType !== 'touch') return
      start.current = { x: event.clientX, y: event.clientY }
      cancel()
      timer.current = setTimeout(() => {
        fired.current = true
        onLongPress()
      }, LONG_PRESS_MS)
    },
    onPointerMove: (event) => {
      if (!timer.current || !start.current) return
      const dx = Math.abs(event.clientX - start.current.x)
      const dy = Math.abs(event.clientY - start.current.y)
      if (dx > MOVE_TOLERANCE_PX || dy > MOVE_TOLERANCE_PX) cancel()
    },
    onPointerUp: cancel,
    onPointerCancel: cancel,
    onContextMenu: (event) => {
      // Android opens the link's context menu on a long press: ours instead.
      if (timer.current || fired.current) event.preventDefault()
    },
    onClick: (event) => {
      if (fired.current) {
        event.preventDefault()
        fired.current = false
      }
    },
  }
}
