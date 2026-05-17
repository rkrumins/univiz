/**
 * App-wide toast notification system.
 *
 * Three exports:
 *   - useToast()         — hook: returns { showToast, dismissToast, showLoading, hideLoading }
 *   - useToastStore      — Zustand store (for direct access outside React)
 *   - <ToastContainer /> — render once in AppLayout; animates all active toasts
 *
 * Toast types:
 *   - success / error / warning / info  — auto-dismiss after 4.5s
 *   - loading                           — persists until explicitly dismissed via hideLoading(key)
 *
 * Usage:
 *   const { showToast, showLoading, hideLoading } = useToast()
 *   showToast('success', 'View saved')
 *   showLoading('assignments', 'Computing layer assignments')
 *   hideLoading('assignments')
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { create } from 'zustand'
import { AnimatePresence, motion } from 'framer-motion'
import { CheckCircle2, AlertCircle, AlertTriangle, Info, Loader2, X } from 'lucide-react'
import { cn } from '@/lib/utils'

// ─── Types ────────────────────────────────────────────────────────────────

export type ToastType = 'success' | 'error' | 'warning' | 'info' | 'loading'

export interface Toast {
  id: number
  type: ToastType
  message: string
  /** Stable key for loading toasts — used by hideLoading() to dismiss. */
  key?: string
  action?: { label: string; onClick: () => void }
  /** Epoch-ms at which the toast was added — drives the progress bar and
   * dismiss timer against an immutable reference time so sibling removals
   * never restart this toast's countdown. */
  createdAt: number
}

// ─── Store ────────────────────────────────────────────────────────────────

interface ToastState {
  toasts: Toast[]
  _nextId: number
  addToast: (toast: Omit<Toast, 'id' | 'createdAt'>) => number
  removeToast: (id: number) => void
  removeByKey: (key: string) => void
}

export const useToastStore = create<ToastState>((set, get) => ({
  toasts: [],
  _nextId: 1,
  addToast: (toast) => {
    const id = get()._nextId
    // For loading toasts with a key, replace any existing toast with that key
    // so we don't stack duplicates for the same operation.
    set(state => ({
      _nextId: state._nextId + 1,
      toasts: [
        ...state.toasts.filter(t => !(toast.key && t.key === toast.key)),
        { ...toast, id, createdAt: Date.now() },
      ],
    }))
    return id
  },
  removeToast: (id) => set(state => ({
    toasts: state.toasts.filter(t => t.id !== id),
  })),
  removeByKey: (key) => set(state => ({
    toasts: state.toasts.filter(t => t.key !== key),
  })),
}))

// ─── Hook ─────────────────────────────────────────────────────────────────

export function useToast() {
  const addToast = useToastStore(s => s.addToast)
  const removeToast = useToastStore(s => s.removeToast)
  const removeByKey = useToastStore(s => s.removeByKey)

  const showToast = useCallback((
    type: Exclude<ToastType, 'loading'>,
    message: string,
    action?: { label: string; onClick: () => void },
  ) => {
    return addToast({ type, message, action })
  }, [addToast])

  /** Show a persistent loading toast. Stays until hideLoading(key) is called. */
  const showLoading = useCallback((key: string, message: string) => {
    return addToast({ type: 'loading', message, key })
  }, [addToast])

  /** Dismiss a loading toast by key. */
  const hideLoading = useCallback((key: string) => {
    removeByKey(key)
  }, [removeByKey])

  const dismissToast = useCallback((id: number) => {
    removeToast(id)
  }, [removeToast])

  return { showToast, showLoading, hideLoading, dismissToast }
}

/**
 * Declarative loading toast — shows while `isLoading` is true, hides when false.
 * Call at the top of a component to bind a loading operation to the toast system.
 *
 * If `successMessage` is provided, a green success toast fires on the
 * `isLoading: true → false` transition (4.5s auto-dismiss). This gives the
 * user explicit confirmation that the operation completed rather than the
 * loading toast just silently disappearing.
 */
export function useLoadingToast(
  key: string,
  isLoading: boolean,
  message: string,
  successMessage?: string,
) {
  const { showLoading, hideLoading, showToast } = useToast()
  // Tracks whether we previously showed a loading toast for this key.
  // Needed because the effect runs on every dependency change, but we only
  // want to fire success on a genuine true → false transition (not on the
  // initial render where isLoading happens to be false).
  const wasLoadingRef = useRef(false)

  useEffect(() => {
    if (isLoading) {
      showLoading(key, message)
      wasLoadingRef.current = true
    } else {
      hideLoading(key)
      if (wasLoadingRef.current && successMessage) {
        showToast('success', successMessage)
      }
      wasLoadingRef.current = false
    }
    return () => hideLoading(key)
  }, [isLoading, key, message, successMessage, showLoading, hideLoading, showToast])
}

// ─── Visual constants ─────────────────────────────────────────────────────

const DURATION = 4500

const accentColors: Record<ToastType, string> = {
  success: 'bg-emerald-500',
  error: 'bg-red-500',
  warning: 'bg-amber-500',
  info: 'bg-blue-500',
  loading: 'bg-indigo-500',
}

const iconColors: Record<ToastType, string> = {
  success: 'text-emerald-500',
  error: 'text-red-500',
  warning: 'text-amber-500',
  info: 'text-blue-500',
  loading: 'text-indigo-500',
}

const iconComponents: Record<ToastType, React.ComponentType<{ className?: string }>> = {
  success: CheckCircle2,
  error: AlertCircle,
  warning: AlertTriangle,
  info: Info,
  loading: Loader2,
}

// ─── Single Toast ─────────────────────────────────────────────────────────

function ToastItem({ toast }: { toast: Toast }) {
  const removeToast = useToastStore(s => s.removeToast)
  const isLoading = toast.type === 'loading'
  const id = toast.id
  const createdAt = toast.createdAt

  // Initial progress is computed from createdAt so a remount (e.g. caused by
  // React StrictMode double-invocation, or by the parent re-rendering for
  // any unrelated reason) doesn't snap the bar back to 100%.
  const [progress, setProgress] = useState(() => {
    if (isLoading) return 100
    const elapsed = Date.now() - createdAt
    return Math.max(0, 100 - (elapsed / DURATION) * 100)
  })

  useEffect(() => {
    // Loading toasts don't auto-dismiss.
    if (isLoading) return

    // Anchor both the dismiss timer and the progress bar on the immutable
    // createdAt timestamp. Previously the timer was restarted from "now"
    // whenever this effect re-ran (e.g. because a sibling toast dismissed
    // and the parent's onDismiss closure changed), which gave the user the
    // misleading impression that the remaining toasts had reset.
    const remainingMs = Math.max(0, DURATION - (Date.now() - createdAt))
    const timer = setTimeout(() => removeToast(id), remainingMs)
    const interval = setInterval(() => {
      const elapsed = Date.now() - createdAt
      const remaining = Math.max(0, 100 - (elapsed / DURATION) * 100)
      setProgress(remaining)
      if (remaining <= 0) clearInterval(interval)
    }, 30)

    return () => {
      clearTimeout(timer)
      clearInterval(interval)
    }
  }, [createdAt, id, isLoading, removeToast])

  const onDismiss = useCallback(() => removeToast(id), [removeToast, id])

  const Icon = iconComponents[toast.type]

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 24, scale: 0.95 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 12, scale: 0.95 }}
      transition={{ type: 'spring', damping: 20, stiffness: 300 }}
      className={cn(
        'w-80 max-w-sm rounded-xl overflow-hidden pointer-events-auto',
        'bg-white dark:bg-slate-800',
        'border border-slate-200 dark:border-slate-700 shadow-lg shadow-black/15 dark:shadow-black/40',
      )}
    >
      <div className="flex items-center gap-3 px-4 py-3.5">
        <Icon className={cn(
          'w-4.5 h-4.5 flex-shrink-0',
          iconColors[toast.type],
          isLoading && 'animate-spin',
        )} />
        <span className="flex-1 text-sm text-ink leading-snug">{toast.message}</span>
        {toast.action && (
          <button
            onClick={() => { toast.action!.onClick(); onDismiss() }}
            className="flex-shrink-0 px-2.5 py-1 rounded-lg text-xs font-bold text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-950/30 transition-colors"
          >
            {toast.action.label}
          </button>
        )}
        {!isLoading && (
          <button
            onClick={onDismiss}
            className="opacity-40 hover:opacity-100 transition-opacity flex-shrink-0 rounded-md p-0.5 hover:bg-black/5 dark:hover:bg-white/5"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Progress bar — only for auto-dismissing toasts */}
      {!isLoading && (
        <div className="h-0.5 w-full bg-black/5 dark:bg-white/5">
          <div
            className={cn('h-full transition-none rounded-r-full', accentColors[toast.type])}
            style={{ width: `${progress}%`, opacity: 0.6 }}
          />
        </div>
      )}

      {/* Indeterminate bar for loading toasts */}
      {isLoading && (
        <div className="h-0.5 w-full bg-black/5 dark:bg-white/5 overflow-hidden">
          <div
            className={cn('h-full w-1/3 rounded-full', accentColors[toast.type])}
            style={{
              opacity: 0.6,
              animation: 'toast-indeterminate 1.5s ease-in-out infinite',
            }}
          />
          <style>{`@keyframes toast-indeterminate { 0% { transform: translateX(-100%); } 100% { transform: translateX(400%); } }`}</style>
        </div>
      )}
    </motion.div>
  )
}

// ─── Container ────────────────────────────────────────────────────────────

/**
 * Render once at the app root (e.g. AppLayout). Displays all active toasts
 * in a fixed stack at the bottom-right.
 */
export function ToastContainer() {
  const toasts = useToastStore(s => s.toasts)

  return (
    <div className="fixed bottom-6 right-6 z-[80] flex flex-col-reverse gap-2 pointer-events-none">
      <AnimatePresence>
        {toasts.map(toast => (
          <ToastItem key={toast.id} toast={toast} />
        ))}
      </AnimatePresence>
    </div>
  )
}
