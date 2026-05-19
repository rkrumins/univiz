import * as LucideIcons from 'lucide-react'
import type { ComponentType, SVGProps } from 'react'

export type EntityIconComponent = ComponentType<SVGProps<SVGSVGElement> & { className?: string }>

// Lucide icons are forwardRef objects ({ $$typeof, render }); a bare function
// component is also valid. Anything else returned from `LucideIcons[name]`
// (string exports like the `icons` registry, primitive values, undefined)
// would render as a broken element if passed to JSX — fall back to Box.
function isRenderable(value: unknown): value is EntityIconComponent {
    if (typeof value === 'function') return true
    if (typeof value === 'object' && value !== null && '$$typeof' in (value as object)) return true
    return false
}

const Fallback = LucideIcons.Box as EntityIconComponent

/**
 * Resolve a schema-provided Lucide icon name to a renderable component,
 * always falling back to the Box icon for missing / non-component lookups.
 *
 * Centralizes the broken-icon fallback that previously had to be repeated
 * at every render site (and which silently rendered broken when the name
 * resolved to a truthy non-component export).
 */
export function resolveEntityIcon(iconName?: string | null): EntityIconComponent {
    if (!iconName) return Fallback
    const candidate = (LucideIcons as Record<string, unknown>)[iconName]
    return isRenderable(candidate) ? candidate : Fallback
}
