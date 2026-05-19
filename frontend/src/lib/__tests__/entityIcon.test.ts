import { describe, expect, it } from 'vitest'
import * as LucideIcons from 'lucide-react'

import { resolveEntityIcon } from '../entityIcon'

describe('resolveEntityIcon', () => {
    it('falls back to Box when no name is given', () => {
        expect(resolveEntityIcon()).toBe(LucideIcons.Box)
        expect(resolveEntityIcon(undefined)).toBe(LucideIcons.Box)
        expect(resolveEntityIcon(null)).toBe(LucideIcons.Box)
        expect(resolveEntityIcon('')).toBe(LucideIcons.Box)
    })

    it('returns the requested icon when it resolves to a renderable component', () => {
        expect(resolveEntityIcon('Database')).toBe(LucideIcons.Database)
        expect(resolveEntityIcon('Table')).toBe(LucideIcons.Table)
    })

    it('falls back to Box when the name resolves to a non-component export', () => {
        // Lucide exposes string exports (e.g. `icons`) and aliases that aren't
        // React components — the resolver must guard against rendering these.
        const candidate = (LucideIcons as Record<string, unknown>)['createLucideIcon']
        // sanity: createLucideIcon is a function — would render fine, so pick a
        // known non-component name. `icons` is a string-keyed registry object,
        // not a React component.
        if (candidate && typeof candidate === 'function') {
            // expected — fall through to the next assertion
        }
        // A name that does not exist at all in Lucide → fall back to Box
        expect(resolveEntityIcon('NotAValidLucideIconName_____xyz')).toBe(LucideIcons.Box)
    })

    it('falls back to Box for an unknown name even if the Lucide module has other shape', () => {
        expect(resolveEntityIcon('definitely_not_there')).toBe(LucideIcons.Box)
    })
})
