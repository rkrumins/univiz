/**
 * GlobalAnnouncementBanner — full-width, high-visibility banners pinned to
 * the very top of the viewport (above TopBar).
 *
 * - Polling interval is admin-configurable (fetched from backend config).
 * - Users CANNOT permanently dismiss — only snooze for admin-configured duration.
 * - After snooze expires, the banner automatically reappears.
 */
import { useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { AlertTriangle, CheckCircle, ArrowRight, Sparkles, PauseCircle } from 'lucide-react'
import { usePolling } from '@/hooks/usePolling'
import { useAnnouncementStore } from '@/store/announcements'

const SNOOZE_TICK = 1000 // re-check snooze expiry every second

const BANNER_CONFIG = {
  info: {
    bar: 'bg-gradient-to-r from-indigo-600 via-violet-600 to-indigo-600',
    icon: Sparkles,
    text: 'text-white',
    muted: 'text-indigo-100',
    cta: 'bg-white/20 hover:bg-white/30 text-white border border-white/20',
    snooze: 'text-white/60 hover:text-white hover:bg-white/10',
    dot: 'bg-indigo-300',
  },
  warning: {
    bar: 'bg-gradient-to-r from-amber-500 via-orange-500 to-amber-500',
    icon: AlertTriangle,
    text: 'text-white',
    muted: 'text-amber-100',
    cta: 'bg-white/20 hover:bg-white/30 text-white border border-white/20',
    snooze: 'text-white/60 hover:text-white hover:bg-white/10',
    dot: 'bg-amber-200',
  },
  success: {
    bar: 'bg-gradient-to-r from-emerald-600 via-teal-600 to-emerald-600',
    icon: CheckCircle,
    text: 'text-white',
    muted: 'text-emerald-100',
    cta: 'bg-white/20 hover:bg-white/30 text-white border border-white/20',
    snooze: 'text-white/60 hover:text-white hover:bg-white/10',
    dot: 'bg-emerald-300',
  },
} as const

/** Format remaining snooze time for the button label. */
function formatSnoozeLabel(minutes: number): string {
  if (minutes < 1) return 'Snooze'
  if (minutes < 60) return `Snooze ${minutes}m`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m > 0 ? `Snooze ${h}h ${m}m` : `Snooze ${h}h`
}

export function GlobalAnnouncementBanner() {
  const { announcements, snoozedUntil, pollIntervalSeconds, fetchActive, fetchConfig, snooze } = useAnnouncementStore()
  const [, setTick] = useState(0) // force re-render to check snooze expiry

  // Fetch config on mount (polling interval, default snooze)
  useEffect(() => {
    fetchConfig()
  }, [fetchConfig])

  // Poll for announcements with jitter + Page Visibility pause via the
  // shared hook. ``pollIntervalSeconds`` comes from the admin config
  // and changes when ops dials it remotely; usePolling re-arms its
  // timer on dep change. WS-2's ETag headers mean ~95% of these
  // poll requests become 304 No-Body in steady state.
  usePolling(fetchActive, pollIntervalSeconds * 1000)

  // Tick every second to re-evaluate snooze expiry
  useEffect(() => {
    const hasSnoozed = Object.keys(snoozedUntil).length > 0
    if (!hasSnoozed) return
    const id = setInterval(() => setTick((t) => t + 1), SNOOZE_TICK)
    return () => clearInterval(id)
  }, [snoozedUntil])

  const now = Date.now()
  const visible = announcements.filter((a) => {
    const expiresAt = snoozedUntil[a.id]
    if (expiresAt && now < expiresAt) return false // currently snoozed
    return true
  })

  if (visible.length === 0) return null

  return (
    <AnimatePresence initial={false}>
      {visible.map((ann) => {
        const cfg = BANNER_CONFIG[ann.bannerType] ?? BANNER_CONFIG.info
        const Icon = cfg.icon
        const canSnooze = ann.snoozeDurationMinutes > 0

        return (
          <motion.div
            key={ann.id}
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ type: 'spring', stiffness: 500, damping: 40 }}
            className="shrink-0 overflow-hidden"
          >
            <div className={`relative ${cfg.bar}`}>
              {/* Subtle animated shimmer overlay */}
              <div className="absolute inset-0 bg-[linear-gradient(110deg,transparent_25%,rgba(255,255,255,0.08)_50%,transparent_75%)] bg-[length:250%_100%] animate-[shimmer_8s_ease-in-out_infinite]" />

              <div className="relative z-10 px-4 py-2.5">
                <div className="flex items-center justify-center gap-3 max-w-screen-2xl mx-auto">
                  {/* Icon + pulse dot */}
                  <span className="relative shrink-0 flex items-center justify-center">
                    <Icon className={`w-4 h-4 ${cfg.text}`} />
                    <span className={`absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full ${cfg.dot} animate-pulse`} />
                  </span>

                  {/* Title + message — centred */}
                  <div className="flex items-center gap-2 flex-wrap justify-center text-center min-w-0">
                    <span className={`text-sm font-bold tracking-wide ${cfg.text}`}>
                      {ann.title}
                    </span>
                    <span className={`hidden sm:inline text-sm ${cfg.muted}`}>
                      —
                    </span>
                    <span className={`text-sm font-medium ${cfg.muted}`}>
                      {ann.message}
                    </span>
                  </div>

                  {/* CTA button */}
                  {ann.ctaText && ann.ctaUrl && (
                    <a
                      href={ann.ctaUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={`shrink-0 inline-flex items-center gap-1.5 px-3.5 py-1 rounded-full text-xs font-bold uppercase tracking-wider transition-all backdrop-blur-sm ${cfg.cta}`}
                    >
                      {ann.ctaText}
                      <ArrowRight className="w-3 h-3" />
                    </a>
                  )}

                  {/* Snooze button — only when admin allows it */}
                  {canSnooze && (
                    <button
                      onClick={() => snooze(ann.id, ann.snoozeDurationMinutes)}
                      className={`shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${cfg.snooze}`}
                      aria-label={`Snooze for ${ann.snoozeDurationMinutes} minutes`}
                      title={`Hide for ${ann.snoozeDurationMinutes} minutes — it will reappear after`}
                    >
                      <PauseCircle className="w-3.5 h-3.5" />
                      {formatSnoozeLabel(ann.snoozeDurationMinutes)}
                    </button>
                  )}
                </div>
              </div>
            </div>
          </motion.div>
        )
      })}
    </AnimatePresence>
  )
}
