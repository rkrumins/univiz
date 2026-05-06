/**
 * CommandPalette - Power user command interface (⌘K)
 * 
 * A Spotlight/VSCode-style command palette for:
 * - Quick entity creation
 * - Navigation between views
 * - Running actions
 * - Searching entities
 */

import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import { useEntityTypes, useSchemaViews } from '@/store/schema'
import { useCanvasStore } from '@/store/canvas'
import { useViewEntityTypes } from '@/hooks/useViewSchema'
import { useEntitySearch, type EntitySearchScope } from '@/hooks/useEntitySearch'
import { useRecentSearches } from '@/hooks/useRecentSearches'
import type { EntitySearchHit } from '@/providers/GraphDataProvider'

// ============================================
// Types
// ============================================

export interface CommandItem {
    id: string
    label: string
    description?: string
    icon: keyof typeof LucideIcons
    category: 'action' | 'create' | 'navigation' | 'entity'
    shortcut?: string
    keywords?: string[]
    onSelect: () => void
}

export interface CommandPaletteProps {
    /** Whether the palette is open */
    isOpen: boolean
    /** Close handler */
    onClose: () => void
    /** Additional commands */
    commands?: CommandItem[]
    /** Action handlers */
    onCreateEntity?: (typeId: string) => void
    onNavigateTo?: (viewId: string) => void
    /**
     * Backwards-compatible "select" callback — fires for any entity hit.
     * Receives only the URN; canvases that need ancestor-aware navigation
     * should wire `onSelectHit` instead.
     */
    onSelectEntity?: (entityId: string) => void
    /**
     * New ancestor-aware selection callback. Fired when a result in
     * `find` mode is chosen. Receives the full hit (node + ancestor chain
     * + match info) so the canvas can hydrate, expand, and centre.
     */
    onSelectHit?: (hit: EntitySearchHit) => void
    onRunAction?: (actionId: string) => void
    /** Default scope for the find-mode entity search. */
    searchScope?: EntitySearchScope
    /** View id passed through to the entity-search backend when scope is 'view'. */
    viewId?: string | null
    /**
     * Mode to land in each time the palette opens. Defaults to 'all' so
     * Cmd+K behaves like a generic command launcher; canvases that wire a
     * dedicated "Search entities" button pass `'find'` for that affordance.
     */
    initialMode?: 'all' | 'create' | 'goto' | 'find'
}

// ============================================
// Component
// ============================================

export function CommandPalette({
    isOpen,
    onClose,
    commands = [],
    onCreateEntity,
    onNavigateTo,
    onSelectEntity,
    onSelectHit,
    onRunAction,
    searchScope = 'view',
    viewId = null,
    initialMode = 'all',
}: CommandPaletteProps) {
    const containerRef = useRef<HTMLDivElement>(null)
    const inputRef = useRef<HTMLInputElement>(null)

    const entityTypes = useEntityTypes()
    const viewEntityTypes = useViewEntityTypes()
    const nodes = useCanvasStore(s => s.nodes)
    const views = useSchemaViews()
    const recentSearches = useRecentSearches()

    const [query, setQuery] = useState('')
    const [highlightedIndex, setHighlightedIndex] = useState(0)
    const [mode, setMode] = useState<'all' | 'create' | 'goto' | 'find'>(initialMode)

    // Backend-driven entity search powering find mode. Stays idle when the
    // user is in another mode — the hook returns immediately on empty query.
    const entitySearch = useEntitySearch({
        initialScope: searchScope,
        viewId,
    })

    // Sync the input's query into the search hook only while find mode is active.
    useEffect(() => {
        if (mode === 'find') entitySearch.setQuery(query)
    // entitySearch.setQuery is stable (useCallback inside the hook); avoid extra rerenders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [query, mode])

    // Build a lookup so result rows can render entity-type colour + icon
    // straight from the active ontology (no string-matching).
    const entityTypeIndex = useMemo(() => {
        const out = new Map<string, { color: string; icon: string }>()
        for (const t of viewEntityTypes) {
            out.set(t.id, {
                color: t.visual?.color ?? '#6b7280',
                icon: t.visual?.icon ?? 'Box',
            })
        }
        return out
    }, [viewEntityTypes])
    
    // Build all available commands
    const allCommands = useMemo((): CommandItem[] => {
        const items: CommandItem[] = []
        
        // Built-in actions
        items.push({
            id: 'action:select-all',
            label: 'Select All',
            description: 'Select all nodes on canvas',
            icon: 'CheckSquare',
            category: 'action',
            shortcut: '⌘A',
            keywords: ['select', 'all', 'nodes'],
            onSelect: () => {
                const allIds = nodes.map(n => n.id)
                allIds.forEach(id => useCanvasStore.getState().selectNode(id, true))
                onClose()
            }
        })
        
        items.push({
            id: 'action:clear-selection',
            label: 'Clear Selection',
            description: 'Deselect all nodes',
            icon: 'X',
            category: 'action',
            shortcut: 'Esc',
            keywords: ['clear', 'deselect', 'cancel'],
            onSelect: () => {
                useCanvasStore.getState().clearSelection()
                onClose()
            }
        })
        
        items.push({
            id: 'action:fit-view',
            label: 'Fit to View',
            description: 'Zoom to fit all nodes',
            icon: 'Maximize',
            category: 'action',
            keywords: ['fit', 'zoom', 'view', 'center'],
            onSelect: () => {
                onRunAction?.('fit-view')
                onClose()
            }
        })
        
        items.push({
            id: 'action:toggle-minimap',
            label: 'Toggle Minimap',
            description: 'Show/hide minimap overlay',
            icon: 'Map',
            category: 'action',
            keywords: ['minimap', 'map', 'overview'],
            onSelect: () => {
                onRunAction?.('toggle-minimap')
                onClose()
            }
        })
        
        // Create entity commands
        entityTypes.forEach(type => {
            items.push({
                id: `create:${type.id}`,
                label: `Create ${type.name}`,
                description: type.description,
                icon: (type.visual?.icon as keyof typeof LucideIcons) || 'Plus',
                category: 'create',
                keywords: ['create', 'new', type.name.toLowerCase(), type.id],
                onSelect: () => {
                    onCreateEntity?.(type.id)
                    onClose()
                }
            })
        })
        
        // Navigation commands
        views.forEach(view => {
            items.push({
                id: `goto:${view.id}`,
                label: `Go to ${view.name}`,
                description: `Switch to ${view.name} view`,
                icon: 'ArrowRight',
                category: 'navigation',
                keywords: ['go', 'view', 'navigate', view.name.toLowerCase()],
                onSelect: () => {
                    onNavigateTo?.(view.id)
                    onClose()
                }
            })
        })
        
        // Add custom commands
        items.push(...commands)

        return items
    }, [entityTypes, nodes, views, commands, onCreateEntity, onNavigateTo, onRunAction, onClose])
    
    // Filter commands based on query and mode. In find mode the result list
    // comes from the backend (see entitySearch.results) so this returns
    // empty — keyboard nav routes through `findHits` instead.
    const filteredCommands = useMemo(() => {
        if (mode === 'find') return []

        let items = allCommands
        if (mode === 'create') {
            items = items.filter(c => c.category === 'create')
        } else if (mode === 'goto') {
            items = items.filter(c => c.category === 'navigation')
        }

        if (query.trim()) {
            const q = query.toLowerCase()
            items = items.filter(c =>
                c.label.toLowerCase().includes(q) ||
                c.description?.toLowerCase().includes(q) ||
                c.keywords?.some(k => k.includes(q))
            )
        }

        return items.slice(0, 12)
    }, [allCommands, query, mode])

    // Keyboard nav target: command list normally; entity-search hits in find mode.
    const findHits = entitySearch.results
    const navItemCount = mode === 'find' ? findHits.length : filteredCommands.length

    const selectFindHit = useCallback((hit: EntitySearchHit) => {
        recentSearches.record(query.trim())
        if (onSelectHit) {
            onSelectHit(hit)
        } else {
            // Backwards-compat path: bare URN selection.
            useCanvasStore.getState().selectNode(hit.node.urn)
            onSelectEntity?.(hit.node.urn)
        }
        onClose()
    }, [onSelectHit, onSelectEntity, onClose, recentSearches, query])
    
    // Reset state when opened — honours the caller's chosen initialMode so
    // canvases can open the palette directly into find mode from a header
    // button without forcing the user to type "/".
    useEffect(() => {
        if (isOpen) {
            setQuery('')
            setHighlightedIndex(0)
            setMode(initialMode)
            setTimeout(() => inputRef.current?.focus(), 50)
        }
    }, [isOpen, initialMode])
    
    // Click outside to close
    useEffect(() => {
        if (!isOpen) return
        
        const handleClickOutside = (e: MouseEvent) => {
            if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
                onClose()
            }
        }
        
        const handleEscape = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                onClose()
            }
        }
        
        setTimeout(() => {
            document.addEventListener('mousedown', handleClickOutside)
            document.addEventListener('keydown', handleEscape)
        }, 0)
        
        return () => {
            document.removeEventListener('mousedown', handleClickOutside)
            document.removeEventListener('keydown', handleEscape)
        }
    }, [isOpen, onClose])
    
    // Keyboard navigation
    const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
        if (e.key === 'ArrowDown') {
            e.preventDefault()
            setHighlightedIndex(i => Math.min(i + 1, Math.max(0, navItemCount - 1)))
        } else if (e.key === 'ArrowUp') {
            e.preventDefault()
            setHighlightedIndex(i => Math.max(i - 1, 0))
        } else if (e.key === 'Enter') {
            e.preventDefault()
            if (mode === 'find') {
                const hit = findHits[highlightedIndex]
                if (hit) selectFindHit(hit)
            } else if (filteredCommands[highlightedIndex]) {
                filteredCommands[highlightedIndex].onSelect()
            }
        } else if (e.key === 'Tab') {
            e.preventDefault()
            // Cycle through modes
            setMode(m => {
                if (m === 'all') return 'create'
                if (m === 'create') return 'goto'
                if (m === 'goto') return 'find'
                return 'all'
            })
        }
    }, [filteredCommands, findHits, highlightedIndex, mode, navItemCount, selectFindHit])
    
    // Check for mode prefix in query
    useEffect(() => {
        if (query.startsWith('>')) {
            setMode('create')
            setQuery(q => q.slice(1))
        } else if (query.startsWith('@')) {
            setMode('goto')
            setQuery(q => q.slice(1))
        } else if (query.startsWith('/')) {
            setMode('find')
            setQuery(q => q.slice(1))
        }
    }, [query])
    
    // Reset highlighted index when the active result list changes
    useEffect(() => {
        setHighlightedIndex(0)
    }, [filteredCommands.length, findHits.length, mode])
    
    // Get icon component
    const getIcon = (iconName: keyof typeof LucideIcons) => {
        const IconComponent = LucideIcons[iconName] as React.ComponentType<{ className?: string }>
        return IconComponent ? <IconComponent className="w-4 h-4" /> : null
    }
    
    // Category styling
    const getCategoryColor = (category: CommandItem['category']) => {
        switch (category) {
            case 'action': return 'text-blue-500'
            case 'create': return 'text-green-500'
            case 'navigation': return 'text-purple-500'
            case 'entity': return 'text-amber-500'
            default: return 'text-ink-muted'
        }
    }
    
    if (!isOpen) return null
    
    return (
        <AnimatePresence>
            <div className="fixed inset-0 z-[300] flex items-start justify-center pt-[15vh]">
                {/* Backdrop */}
                <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="absolute inset-0 bg-black/40"
                    onClick={onClose}
                />
                
                {/* Palette */}
                <motion.div
                    ref={containerRef}
                    initial={{ opacity: 0, scale: 0.95, y: -20 }}
                    animate={{ opacity: 1, scale: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95, y: -20 }}
                    transition={{ duration: 0.15, ease: 'easeOut' }}
                    className={cn(
                        "relative w-full max-w-[560px]",
                        "bg-canvas-elevated/98 backdrop-blur-xl",
                        "border border-glass-border rounded-2xl shadow-lg",
                        "overflow-hidden"
                    )}
                >
                    {/* Search Input */}
                    <div className="flex items-center gap-3 px-4 py-4 border-b border-glass-border">
                        <div className="flex items-center gap-2">
                            <LucideIcons.Command className="w-5 h-5 text-accent-lineage" />
                        </div>
                        
                        {/* Mode Badges */}
                        {mode !== 'all' && (
                            <button
                                onClick={() => setMode('all')}
                                className={cn(
                                    "flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium",
                                    mode === 'create' && "bg-green-500/10 text-green-600",
                                    mode === 'goto' && "bg-purple-500/10 text-purple-600",
                                    mode === 'find' && "bg-amber-500/10 text-amber-600"
                                )}
                            >
                                {mode === 'create' && <><LucideIcons.Plus className="w-3 h-3" /> Create</>}
                                {mode === 'goto' && <><LucideIcons.ArrowRight className="w-3 h-3" /> Go to</>}
                                {mode === 'find' && <><LucideIcons.Search className="w-3 h-3" /> Find</>}
                                <LucideIcons.X className="w-3 h-3 ml-1" />
                            </button>
                        )}
                        
                        <input
                            ref={inputRef}
                            type="text"
                            value={query}
                            onChange={(e) => setQuery(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder={
                                mode === 'all' ? "Type a command or search..." :
                                mode === 'create' ? "What do you want to create?" :
                                mode === 'goto' ? "Where do you want to go?" :
                                "Search entities by name, URN, tag, or property…"
                            }
                            className={cn(
                                "flex-1 bg-transparent outline-none text-ink",
                                "placeholder:text-ink-muted text-base"
                            )}
                        />

                        {mode === 'find' && entitySearch.isLoading && (
                            <LucideIcons.Loader2 className="w-4 h-4 text-accent-lineage animate-spin" aria-label="Searching" />
                        )}

                        {mode === 'find' && (
                            <button
                                type="button"
                                onClick={() => entitySearch.setScope(entitySearch.scope === 'view' ? 'global' : 'view')}
                                className={cn(
                                    "flex items-center gap-1 px-2 py-0.5 rounded-md text-2xs font-medium transition-colors",
                                    entitySearch.scope === 'view'
                                        ? "bg-accent-lineage/10 text-accent-lineage"
                                        : "bg-black/5 dark:bg-white/10 text-ink-muted"
                                )}
                                title="Toggle search scope"
                            >
                                <LucideIcons.Filter className="w-3 h-3" />
                                {entitySearch.scope === 'view' ? 'This view' : 'All entities'}
                            </button>
                        )}

                        <kbd className="px-2 py-1 rounded bg-black/10 dark:bg-white/10 text-[10px] font-mono text-ink-muted">
                            Esc
                        </kbd>
                    </div>
                    
                    {/* Results */}
                    <div className="max-h-[400px] overflow-y-auto custom-scrollbar">
                        {mode === 'find' ? (
                            <FindModeResults
                                hits={findHits}
                                isLoading={entitySearch.isLoading}
                                error={entitySearch.error}
                                hasMore={entitySearch.hasMore}
                                tookMs={entitySearch.tookMs}
                                query={query}
                                highlightedIndex={highlightedIndex}
                                entityTypeIndex={entityTypeIndex}
                                recents={recentSearches.recents}
                                onSelect={selectFindHit}
                                onChooseRecent={(q) => setQuery(q)}
                                onClearRecents={recentSearches.clear}
                                onFetchMore={entitySearch.fetchMore}
                            />
                        ) : filteredCommands.length > 0 ? (
                            <div className="py-2">
                                {filteredCommands.map((cmd, index) => (
                                    <button
                                        key={cmd.id}
                                        onClick={cmd.onSelect}
                                        className={cn(
                                            "w-full flex items-center gap-3 px-4 py-3 text-left transition-colors",
                                            index === highlightedIndex
                                                ? "bg-accent-lineage/10"
                                                : "hover:bg-black/5 dark:hover:bg-white/5"
                                        )}
                                    >
                                        <div className={cn(
                                            "w-8 h-8 rounded-lg flex items-center justify-center",
                                            "bg-black/5 dark:bg-white/5",
                                            getCategoryColor(cmd.category)
                                        )}>
                                            {getIcon(cmd.icon)}
                                        </div>

                                        <div className="flex-1 min-w-0">
                                            <div className="text-sm font-medium text-ink truncate">
                                                {cmd.label}
                                            </div>
                                            {cmd.description && (
                                                <div className="text-xs text-ink-muted truncate">
                                                    {cmd.description}
                                                </div>
                                            )}
                                        </div>

                                        {cmd.shortcut && (
                                            <kbd className="px-2 py-1 rounded bg-black/10 dark:bg-white/10 text-[10px] font-mono text-ink-muted">
                                                {cmd.shortcut}
                                            </kbd>
                                        )}
                                    </button>
                                ))}
                            </div>
                        ) : (
                            <div className="py-12 text-center">
                                <LucideIcons.Search className="w-10 h-10 text-ink-muted/30 mx-auto mb-3" />
                                <p className="text-sm text-ink-muted">No results found</p>
                                <p className="text-xs text-ink-muted/70 mt-1">Try a different search term</p>
                            </div>
                        )}
                    </div>
                    
                    {/* Footer */}
                    <div className="flex items-center justify-between px-4 py-2 border-t border-glass-border bg-black/5 dark:bg-white/5">
                        <div className="flex items-center gap-3">
                            <span className="text-[10px] text-ink-muted flex items-center gap-1">
                                <kbd className="px-1 rounded bg-black/10 dark:bg-white/10 font-mono">↑↓</kbd> Navigate
                            </span>
                            <span className="text-[10px] text-ink-muted flex items-center gap-1">
                                <kbd className="px-1 rounded bg-black/10 dark:bg-white/10 font-mono">↵</kbd> Select
                            </span>
                            <span className="text-[10px] text-ink-muted flex items-center gap-1">
                                <kbd className="px-1 rounded bg-black/10 dark:bg-white/10 font-mono">Tab</kbd> Mode
                            </span>
                        </div>
                        <div className="flex items-center gap-2">
                            <span className="text-[10px] text-ink-muted">
                                <kbd className="px-1 rounded bg-black/10 dark:bg-white/10 font-mono">&gt;</kbd> Create
                            </span>
                            <span className="text-[10px] text-ink-muted">
                                <kbd className="px-1 rounded bg-black/10 dark:bg-white/10 font-mono">@</kbd> Go to
                            </span>
                            <span className="text-[10px] text-ink-muted">
                                <kbd className="px-1 rounded bg-black/10 dark:bg-white/10 font-mono">/</kbd> Find
                            </span>
                        </div>
                    </div>
                </motion.div>
            </div>
        </AnimatePresence>
    )
}

export default CommandPalette

// ============================================
// Find Mode — backend-driven entity search results
// ============================================

interface FindModeResultsProps {
    hits: EntitySearchHit[]
    isLoading: boolean
    error: Error | null
    hasMore: boolean
    tookMs: number
    query: string
    highlightedIndex: number
    entityTypeIndex: Map<string, { color: string; icon: string }>
    recents: string[]
    onSelect: (hit: EntitySearchHit) => void
    onChooseRecent: (q: string) => void
    onClearRecents: () => void
    onFetchMore: () => void
}

function FindModeResults({
    hits, isLoading, error, hasMore, tookMs, query, highlightedIndex,
    entityTypeIndex, recents, onSelect, onChooseRecent, onClearRecents, onFetchMore,
}: FindModeResultsProps) {
    const trimmed = query.trim()

    if (error) {
        return (
            <div className="py-12 text-center px-6">
                <LucideIcons.AlertCircle className="w-10 h-10 text-red-400 mx-auto mb-3" />
                <p className="text-sm text-ink">Search failed</p>
                <p className="text-xs text-ink-muted/80 mt-1">{error.message}</p>
            </div>
        )
    }

    if (!trimmed) {
        // Idle state — surface recent searches if we have any.
        if (recents.length === 0) {
            return (
                <div className="py-12 text-center px-6">
                    <LucideIcons.Search className="w-10 h-10 text-ink-muted/30 mx-auto mb-3" />
                    <p className="text-sm text-ink-muted">Search across the entire graph</p>
                    <p className="text-xs text-ink-muted/70 mt-1">By name, URN, tag, or property value</p>
                </div>
            )
        }
        return (
            <div className="py-2">
                <div className="flex items-center justify-between px-4 py-1.5">
                    <span className="text-2xs font-semibold text-ink-muted uppercase tracking-wider">Recent</span>
                    <button
                        type="button"
                        onClick={onClearRecents}
                        className="text-2xs text-ink-muted hover:text-ink"
                    >
                        Clear
                    </button>
                </div>
                {recents.map((r) => (
                    <button
                        key={r}
                        onClick={() => onChooseRecent(r)}
                        className="w-full flex items-center gap-3 px-4 py-2 text-left hover:bg-black/5 dark:hover:bg-white/5 transition-colors"
                    >
                        <LucideIcons.Clock className="w-3.5 h-3.5 text-ink-muted" />
                        <span className="text-sm text-ink">{r}</span>
                    </button>
                ))}
            </div>
        )
    }

    if (isLoading && hits.length === 0) {
        return (
            <div className="py-2">
                {[0, 1, 2].map(i => (
                    <div key={i} className="flex items-center gap-3 px-4 py-3 animate-pulse">
                        <div className="w-8 h-8 rounded-lg bg-black/5 dark:bg-white/5" />
                        <div className="flex-1">
                            <div className="h-3 w-2/3 rounded bg-black/5 dark:bg-white/5 mb-1.5" />
                            <div className="h-2.5 w-1/2 rounded bg-black/5 dark:bg-white/5" />
                        </div>
                    </div>
                ))}
            </div>
        )
    }

    if (hits.length === 0) {
        return (
            <div className="py-12 text-center px-6">
                <LucideIcons.Search className="w-10 h-10 text-ink-muted/30 mx-auto mb-3" />
                <p className="text-sm text-ink-muted">No entities match &ldquo;{trimmed}&rdquo;</p>
                <p className="text-xs text-ink-muted/70 mt-1">Try fewer characters or a different field</p>
            </div>
        )
    }

    return (
        <div className="py-2">
            {hits.map((hit, index) => (
                <FindModeResultRow
                    key={hit.node.urn}
                    hit={hit}
                    isHighlighted={index === highlightedIndex}
                    query={trimmed}
                    typeVisual={entityTypeIndex.get(hit.node.entityType)}
                    onSelect={onSelect}
                />
            ))}

            {hasMore && (
                <button
                    type="button"
                    onClick={onFetchMore}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-xs text-accent-lineage hover:bg-accent-lineage/5 transition-colors"
                >
                    <LucideIcons.ChevronDown className="w-3.5 h-3.5" />
                    Load more
                </button>
            )}

            <div className="px-4 py-1.5 text-2xs text-ink-muted/60 text-right">
                {hits.length} result{hits.length === 1 ? '' : 's'} · {tookMs}ms
            </div>
        </div>
    )
}

interface FindModeResultRowProps {
    hit: EntitySearchHit
    isHighlighted: boolean
    query: string
    typeVisual?: { color: string; icon: string }
    onSelect: (hit: EntitySearchHit) => void
}

function FindModeResultRow({ hit, isHighlighted, query, typeVisual, onSelect }: FindModeResultRowProps) {
    const color = typeVisual?.color ?? '#6b7280'
    const IconComponent = typeVisual?.icon
        ? (LucideIcons[typeVisual.icon as keyof typeof LucideIcons] as React.ComponentType<{ className?: string }>) ?? LucideIcons.Box
        : LucideIcons.Box

    const primaryMatch = hit.matches[0]
    const showMatchPill = primaryMatch && primaryMatch.field !== 'displayName'

    return (
        <button
            onClick={() => onSelect(hit)}
            className={cn(
                "w-full flex items-start gap-3 px-4 py-2.5 text-left transition-colors",
                isHighlighted ? "bg-accent-lineage/10" : "hover:bg-black/5 dark:hover:bg-white/5",
            )}
        >
            <div
                className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"
                style={{ backgroundColor: `${color}15`, color }}
            >
                <IconComponent className="w-4 h-4" />
            </div>

            <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-medium text-ink truncate">
                        {highlightMatch(hit.node.displayName || hit.node.urn, query)}
                    </span>
                    {hit.node.tags && hit.node.tags.slice(0, 2).map(tag => (
                        <span
                            key={tag}
                            className="text-2xs px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                            style={{ backgroundColor: `${color}15`, color }}
                        >
                            {tag}
                        </span>
                    ))}
                </div>

                {hit.ancestorChain.length > 0 && (
                    <div className="flex items-center gap-1 mt-0.5 text-2xs text-ink-muted truncate">
                        {hit.ancestorChain.map((anc, i) => (
                            <React.Fragment key={anc.urn}>
                                {i > 0 && <LucideIcons.ChevronRight className="w-2.5 h-2.5 flex-shrink-0 opacity-50" />}
                                <span className="truncate">{anc.displayName || anc.urn}</span>
                            </React.Fragment>
                        ))}
                    </div>
                )}

                {showMatchPill && (
                    <div className="mt-1 text-2xs text-ink-muted/80 truncate">
                        <span className="font-medium text-ink-muted">matched in {primaryMatch.field}:</span>{' '}
                        <span className="italic">{highlightMatch(primaryMatch.snippet, query)}</span>
                    </div>
                )}
            </div>
        </button>
    )
}

/** Wrap occurrences of `query` (case-insensitive) in a styled span. */
function highlightMatch(text: string, query: string): React.ReactNode {
    if (!query) return text
    const lower = text.toLowerCase()
    const q = query.toLowerCase()
    const parts: React.ReactNode[] = []
    let i = 0
    while (i < text.length) {
        const idx = lower.indexOf(q, i)
        if (idx < 0) {
            parts.push(text.slice(i))
            break
        }
        if (idx > i) parts.push(text.slice(i, idx))
        parts.push(
            <mark key={`m-${idx}`} className="bg-accent-lineage/20 text-accent-lineage rounded px-0.5">
                {text.slice(idx, idx + q.length)}
            </mark>,
        )
        i = idx + q.length
    }
    return <>{parts}</>
}

