import { useEffect, useRef } from 'react'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), audio[controls], [tabindex]:not([tabindex="-1"])'

// Page-wide state shared by every open modal. Modals can unmount in any order
// (React cleans up a deleted subtree parent-first), so nothing here depends on
// cleanup order: the scroll lock follows the open count, and `inert` is
// recomputed from the topmost modal each time the stack changes.
const stack = []
const madeInert = new Set()
let savedOverflow = ''

function sync() {
  // A modal whose node already left the DOM (unmounted with its parent, its
  // cleanup still pending) no longer counts.
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    if (!stack[i].isConnected) stack.splice(i, 1)
  }
  madeInert.forEach((el) => {
    el.inert = false
  })
  madeInert.clear()
  const top = stack[stack.length - 1]
  if (!top) return
  for (const el of document.body.children) {
    if (el !== top && !el.inert && el.tagName !== 'SCRIPT') {
      el.inert = true
      madeInert.add(el)
    }
  }
}

function focusables(node) {
  return [...node.querySelectorAll(FOCUSABLE)].filter((el) => el.getClientRects().length)
}

/**
 * Modal behaviour for a node portalled straight into <body>: everything else
 * becomes `inert` (and the page stops scrolling), focus moves into the modal
 * and Tab stays inside it, Escape calls `onClose`, and focus returns to the
 * element that opened it. Stacks: a dialog opened over a sheet makes the
 * sheet inert until it closes; only the topmost modal reacts to keys.
 *
 * Mount the modal only while open. Returns the ref for its root element (give
 * it tabIndex={-1}); an element inside with `data-autofocus` gets the initial
 * focus instead.
 */
export function useModal(onClose) {
  const ref = useRef(null)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
  })

  useEffect(() => {
    const node = ref.current
    if (!node) return undefined

    const previous = document.activeElement
    if (stack.length === 0) {
      savedOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
    stack.push(node)
    sync()
    ;(node.querySelector('[data-autofocus]') ?? node).focus({ preventScroll: true })

    // On the document, not the node: if the focused element is removed (a
    // picked option, a deleted row), focus falls to <body> and keys must
    // still reach the modal.
    function onKeyDown(event) {
      if (stack[stack.length - 1] !== node) return
      if (event.key === 'Escape') {
        event.stopPropagation()
        onCloseRef.current?.()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusables(node)
      if (items.length === 0) {
        event.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (!node.contains(active)) {
        event.preventDefault()
        ;(event.shiftKey ? last : first).focus()
      } else if (event.shiftKey && (active === first || active === node)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)

    return () => {
      document.removeEventListener('keydown', onKeyDown)
      const index = stack.indexOf(node)
      if (index !== -1) stack.splice(index, 1)
      sync()
      if (stack.length === 0) document.body.style.overflow = savedOverflow
      if (previous instanceof HTMLElement && previous.isConnected && !previous.closest('[inert]')) {
        previous.focus({ preventScroll: true })
      }
    }
  }, [])

  return ref
}
