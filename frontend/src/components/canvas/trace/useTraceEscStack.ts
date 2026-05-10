import { useEffect } from 'react'

/**
 * Shared ESC stack — overlays/rows that want to consume ESC register here.
 * The most-recently-opened entry consumes the event; on close, it pops off
 * and the next-most-recent entry takes over.
 *
 * Why a module-level stack and not React state: ESC is a global event that
 * needs to be resolved deterministically regardless of which component
 * subtree the user has focus in. Keying off React render timing or context
 * would fail when the user opens Recent (small popover) and Inspector (big
 * row) simultaneously — both want ESC, only one should fire at a time.
 *
 * Order is explicit via `priority`: higher priority pops first when ties
 * exist. Today: Recent popover priority 100; Inspector row priority 50;
 * any future "trace exit" fallback registered at 0.
 */

interface StackEntry {
  id: number
  priority: number
  onClose: () => void
}

let nextId = 1
const stack: StackEntry[] = []
let listenerAttached = false

function ensureListener() {
  if (listenerAttached) return
  listenerAttached = true
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || stack.length === 0) return
    // Skip when the user is typing — input/textarea/contenteditable own ESC
    // for native form-cancel semantics.
    const t = e.target as HTMLElement | null
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
    // Pop the highest-priority entry; on tie, the most recently registered
    // wins (LIFO within priority).
    let topIdx = -1
    let topPriority = -Infinity
    for (let i = stack.length - 1; i >= 0; i--) {
      if (stack[i].priority > topPriority) {
        topPriority = stack[i].priority
        topIdx = i
      }
    }
    if (topIdx < 0) return
    e.preventDefault()
    e.stopPropagation()
    stack[topIdx].onClose()
  }, true) // capture-phase: beat the canvas-level ESC handler
}

/**
 * Register an ESC handler while `isOpen` is true. Higher-priority
 * registrations consume ESC first.
 */
export function useTraceEscStack(isOpen: boolean, onClose: () => void, priority = 50) {
  useEffect(() => {
    if (!isOpen) return
    ensureListener()
    const entry: StackEntry = { id: nextId++, priority, onClose }
    stack.push(entry)
    return () => {
      const i = stack.findIndex(e => e.id === entry.id)
      if (i >= 0) stack.splice(i, 1)
    }
  }, [isOpen, onClose, priority])
}
