/**
 * PropertyEditor — recursive CRUD editor for arbitrary JSON values.
 *
 * Drives the nested-property editing surface inside EntityDrawer's Edit mode.
 * Pure controlled component: parent owns the value, this component emits a new
 * deep-cloned value on every mutation so referential equality checks in staging
 * stores see a real change.
 */

import { useState, useCallback, useMemo, useRef, useEffect } from 'react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import {
  ChevronDown,
  ChevronRight,
  Plus,
  Trash2,
  Lock,
  GripVertical,
  Code,
  Check,
  X as XIcon,
  Type as TypeIcon,
  Hash,
  ToggleLeft,
  CircleDashed,
  Braces,
  Brackets,
} from 'lucide-react'
import { cn } from '@/lib/utils'

// ============================================
// Types
// ============================================

type ValueKind = 'string' | 'number' | 'boolean' | 'null' | 'object' | 'array'

export interface PropertyEditorProps {
  value: unknown
  onChange: (next: unknown) => void
  /** Keys at the root level rendered with a lock icon, no edit/delete affordances. */
  readOnlyKeys?: string[]
  /** Hide the outer card chrome — useful when embedded directly in a parent section. */
  bare?: boolean
}

const MAX_DEPTH = 6

// ============================================
// Helpers
// ============================================

function kindOf(v: unknown): ValueKind {
  if (v === null) return 'null'
  if (Array.isArray(v)) return 'array'
  return typeof v as ValueKind
}

function coerceValue(value: unknown, target: ValueKind): unknown {
  switch (target) {
    case 'string':
      if (value === null || value === undefined) return ''
      if (typeof value === 'object') return JSON.stringify(value)
      return String(value)
    case 'number': {
      const n = Number(value)
      return Number.isFinite(n) ? n : 0
    }
    case 'boolean':
      if (typeof value === 'string') return value.toLowerCase() === 'true' || value === '1'
      return Boolean(value)
    case 'null':
      return null
    case 'object':
      return typeof value === 'object' && value !== null && !Array.isArray(value) ? value : {}
    case 'array':
      return Array.isArray(value) ? value : []
  }
}

function defaultForKind(kind: ValueKind): unknown {
  switch (kind) {
    case 'string': return ''
    case 'number': return 0
    case 'boolean': return false
    case 'null': return null
    case 'object': return {}
    case 'array': return []
  }
}

const KIND_META: Record<ValueKind, { label: string; icon: typeof TypeIcon; tone: string }> = {
  string: { label: 'str', icon: TypeIcon, tone: 'text-blue-500 bg-blue-500/10' },
  number: { label: 'num', icon: Hash, tone: 'text-purple-500 bg-purple-500/10' },
  boolean: { label: 'bool', icon: ToggleLeft, tone: 'text-amber-500 bg-amber-500/10' },
  null: { label: 'null', icon: CircleDashed, tone: 'text-ink-muted bg-white/5' },
  object: { label: 'obj', icon: Braces, tone: 'text-emerald-500 bg-emerald-500/10' },
  array: { label: 'arr', icon: Brackets, tone: 'text-cyan-500 bg-cyan-500/10' },
}

// ============================================
// Root
// ============================================

export function PropertyEditor({ value, onChange, readOnlyKeys, bare }: PropertyEditorProps) {
  const rootKind = kindOf(value)

  // If the root isn't an object/array, render a single value editor.
  // In practice we always pass an object from EntityDrawer, but keep this safe.
  if (rootKind !== 'object' && rootKind !== 'array') {
    return (
      <PrimitiveValueEditor
        value={value}
        onChange={onChange}
      />
    )
  }

  if (rootKind === 'array') {
    return (
      <ArrayBody
        value={value as unknown[]}
        onChange={onChange as (v: unknown[]) => void}
        depth={0}
        bare={bare}
      />
    )
  }

  return (
    <ObjectBody
      value={value as Record<string, unknown>}
      onChange={onChange as (v: Record<string, unknown>) => void}
      readOnlyKeys={readOnlyKeys}
      depth={0}
      bare={bare}
    />
  )
}

// ============================================
// Object Body
// ============================================

interface ObjectBodyProps {
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
  readOnlyKeys?: string[]
  depth: number
  bare?: boolean
}

function ObjectBody({ value, onChange, readOnlyKeys, depth, bare }: ObjectBodyProps) {
  const [adding, setAdding] = useState(false)
  const entries = Object.entries(value)

  const updateKey = useCallback((oldKey: string, newKey: string) => {
    if (oldKey === newKey || !newKey.trim()) return
    if (Object.prototype.hasOwnProperty.call(value, newKey)) return
    // Preserve insertion order: rebuild object swapping the key in place.
    const next: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value)) {
      next[k === oldKey ? newKey : k] = v
    }
    onChange(next)
  }, [value, onChange])

  const updateValue = useCallback((key: string, newValue: unknown) => {
    onChange({ ...value, [key]: newValue })
  }, [value, onChange])

  const deleteKey = useCallback((key: string) => {
    const { [key]: _removed, ...rest } = value
    onChange(rest)
  }, [value, onChange])

  const addKey = useCallback((key: string, kind: ValueKind) => {
    if (!key.trim()) return
    if (Object.prototype.hasOwnProperty.call(value, key)) return
    onChange({ ...value, [key]: defaultForKind(kind) })
    setAdding(false)
  }, [value, onChange])

  if (entries.length === 0 && !adding) {
    return (
      <div className={cn(!bare && "rounded-lg border border-glass-border/40 bg-black/5 dark:bg-white/[0.02] px-3 py-3")}>
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors"
        >
          <Plus className="w-3.5 h-3.5" />
          Add first property
        </button>
      </div>
    )
  }

  return (
    <div className={cn(
      "space-y-1",
      depth > 0 && "pl-3 border-l border-glass-border/40 ml-1.5",
    )}>
      {entries.map(([key, v]) => (
        <PropertyRow
          key={key}
          rowKey={key}
          value={v}
          readOnly={depth === 0 && readOnlyKeys?.includes(key)}
          depth={depth}
          onKeyChange={(nk) => updateKey(key, nk)}
          onValueChange={(nv) => updateValue(key, nv)}
          onDelete={() => deleteKey(key)}
        />
      ))}
      {adding ? (
        <AddRow
          onAdd={addKey}
          onCancel={() => setAdding(false)}
          existingKeys={Object.keys(value)}
          variant="object"
        />
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors mt-1.5 ml-1"
        >
          <Plus className="w-3.5 h-3.5" />
          Add property
        </button>
      )}
    </div>
  )
}

// ============================================
// Array Body
// ============================================

interface ArrayBodyProps {
  value: unknown[]
  onChange: (next: unknown[]) => void
  depth: number
  bare?: boolean
}

function ArrayBody({ value, onChange, depth, bare }: ArrayBodyProps) {
  const [adding, setAdding] = useState(false)

  // Reorder.Group needs stable item identities — we use string keys derived from
  // index + a per-render salt. To keep DnD stable, attach a refsalt-based key.
  const keyedItems = useMemo(
    () => value.map((v, i) => ({ id: `${i}`, value: v })),
    [value],
  )

  const updateAt = useCallback((index: number, newValue: unknown) => {
    const next = value.slice()
    next[index] = newValue
    onChange(next)
  }, [value, onChange])

  const deleteAt = useCallback((index: number) => {
    onChange(value.filter((_, i) => i !== index))
  }, [value, onChange])

  const addItem = useCallback((_key: string, kind: ValueKind) => {
    onChange([...value, defaultForKind(kind)])
    setAdding(false)
  }, [value, onChange])

  const handleReorder = useCallback((items: typeof keyedItems) => {
    onChange(items.map(item => item.value))
  }, [onChange])

  if (value.length === 0 && !adding) {
    return (
      <div className={cn(!bare && "rounded-lg border border-glass-border/40 bg-black/5 dark:bg-white/[0.02] px-3 py-3")}>
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors"
        >
          <Plus className="w-3.5 h-3.5" />
          Add first item
        </button>
      </div>
    )
  }

  return (
    <div className={cn(
      "space-y-1",
      depth > 0 && "pl-3 border-l border-glass-border/40 ml-1.5",
    )}>
      <Reorder.Group axis="y" values={keyedItems} onReorder={handleReorder} className="space-y-1">
        {keyedItems.map((item, index) => (
          <Reorder.Item
            key={item.id}
            value={item}
            className="list-none"
          >
            <PropertyRow
              rowKey={`[${index}]`}
              value={item.value}
              depth={depth}
              isArrayItem
              onValueChange={(nv) => updateAt(index, nv)}
              onDelete={() => deleteAt(index)}
            />
          </Reorder.Item>
        ))}
      </Reorder.Group>
      {adding ? (
        <AddRow
          onAdd={addItem}
          onCancel={() => setAdding(false)}
          existingKeys={[]}
          variant="array"
        />
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors mt-1.5 ml-1"
        >
          <Plus className="w-3.5 h-3.5" />
          Add item
        </button>
      )}
    </div>
  )
}

// ============================================
// PropertyRow — single key/value row, renders the right body for its kind
// ============================================

interface PropertyRowProps {
  rowKey: string
  value: unknown
  readOnly?: boolean
  isArrayItem?: boolean
  depth: number
  onKeyChange?: (newKey: string) => void
  onValueChange: (newValue: unknown) => void
  onDelete: () => void
}

function PropertyRow({
  rowKey,
  value,
  readOnly,
  isArrayItem,
  depth,
  onKeyChange,
  onValueChange,
  onDelete,
}: PropertyRowProps) {
  const kind = kindOf(value)
  const isContainer = kind === 'object' || kind === 'array'
  const [expanded, setExpanded] = useState(depth < 1)
  const [editingKey, setEditingKey] = useState(false)
  const [keyDraft, setKeyDraft] = useState(rowKey)
  const [rawJsonOpen, setRawJsonOpen] = useState(false)

  useEffect(() => { setKeyDraft(rowKey) }, [rowKey])

  const commitKey = () => {
    setEditingKey(false)
    if (keyDraft !== rowKey && keyDraft.trim()) {
      onKeyChange?.(keyDraft.trim())
    } else {
      setKeyDraft(rowKey)
    }
  }

  const changeKind = (next: ValueKind) => {
    onValueChange(coerceValue(value, next))
  }

  return (
    <div className="group">
      <div className={cn(
        "flex items-center gap-2 py-1.5 px-2 rounded-lg",
        "hover:bg-white/[0.03] transition-colors",
      )}>
        {/* Expand / drag handle */}
        <div className="flex items-center w-5 flex-shrink-0">
          {isContainer ? (
            <button
              onClick={() => setExpanded(e => !e)}
              className="w-5 h-5 flex items-center justify-center text-ink-muted hover:text-ink"
              title={expanded ? 'Collapse' : 'Expand'}
            >
              {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            </button>
          ) : isArrayItem ? (
            <GripVertical className="w-3.5 h-3.5 text-ink-muted/40 cursor-grab" />
          ) : (
            <span className="w-5" />
          )}
        </div>

        {/* Type chip */}
        <TypeChip kind={kind} readOnly={readOnly} onChange={readOnly ? undefined : changeKind} />

        {/* Key */}
        <div className="min-w-0 flex-1 flex items-center gap-2">
          {isArrayItem ? (
            <span className="text-xs font-mono text-ink-muted">{rowKey}</span>
          ) : readOnly ? (
            <span className="flex items-center gap-1.5 text-xs font-mono text-ink-muted truncate" title={rowKey}>
              <Lock className="w-3 h-3 flex-shrink-0" />
              {rowKey}
            </span>
          ) : editingKey ? (
            <input
              autoFocus
              value={keyDraft}
              onChange={(e) => setKeyDraft(e.target.value)}
              onBlur={commitKey}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitKey()
                if (e.key === 'Escape') { setKeyDraft(rowKey); setEditingKey(false) }
              }}
              className="text-xs font-mono px-1.5 py-0.5 rounded bg-white/10 border border-accent-lineage/40 outline-none min-w-0 flex-1"
            />
          ) : (
            <button
              onClick={() => setEditingKey(true)}
              className="text-xs font-mono text-ink hover:text-accent-lineage truncate text-left"
              title="Rename key"
            >
              {rowKey}
            </button>
          )}

          {/* Inline primitive value */}
          {!isContainer && (
            <div className="flex-1 min-w-0 flex justify-end">
              <PrimitiveValueEditor value={value} onChange={onValueChange} readOnly={readOnly} />
            </div>
          )}

          {/* Container summary */}
          {isContainer && (
            <span className="text-2xs text-ink-muted">
              {kind === 'object'
                ? `${Object.keys(value as object).length} keys`
                : `${(value as unknown[]).length} items`}
            </span>
          )}
        </div>

        {/* Row actions */}
        <div className={cn(
          "flex items-center gap-0.5 flex-shrink-0",
          "opacity-0 group-hover:opacity-100 transition-opacity",
        )}>
          {isContainer && depth >= MAX_DEPTH - 1 && (
            <button
              onClick={() => setRawJsonOpen(true)}
              className="w-6 h-6 flex items-center justify-center rounded text-ink-muted hover:text-ink hover:bg-white/10"
              title="Edit as raw JSON"
            >
              <Code className="w-3 h-3" />
            </button>
          )}
          {!readOnly && (
            <button
              onClick={onDelete}
              className="w-6 h-6 flex items-center justify-center rounded text-ink-muted hover:text-red-500 hover:bg-red-500/10"
              title="Delete"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>

      {/* Container body */}
      {isContainer && expanded && depth < MAX_DEPTH - 1 && (
        <div className="ml-5 mb-1">
          {kind === 'object' ? (
            <ObjectBody
              value={value as Record<string, unknown>}
              onChange={onValueChange as (v: Record<string, unknown>) => void}
              depth={depth + 1}
            />
          ) : (
            <ArrayBody
              value={value as unknown[]}
              onChange={onValueChange as (v: unknown[]) => void}
              depth={depth + 1}
            />
          )}
        </div>
      )}

      {/* Raw-JSON fallback at max depth */}
      {isContainer && expanded && depth >= MAX_DEPTH - 1 && (
        <div className="ml-5 mb-1">
          <RawJsonEditor
            value={value}
            onChange={onValueChange}
            isOpen={true}
            onClose={() => setRawJsonOpen(false)}
            inline
          />
        </div>
      )}

      {/* Modal raw-JSON popover (only when explicitly opened from action) */}
      {rawJsonOpen && depth < MAX_DEPTH - 1 && (
        <RawJsonEditor
          value={value}
          onChange={onValueChange}
          isOpen={rawJsonOpen}
          onClose={() => setRawJsonOpen(false)}
        />
      )}
    </div>
  )
}

// ============================================
// PrimitiveValueEditor
// ============================================

function PrimitiveValueEditor({
  value,
  onChange,
  readOnly,
}: {
  value: unknown
  onChange: (next: unknown) => void
  readOnly?: boolean
}) {
  const kind = kindOf(value)

  if (kind === 'null') {
    return <span className="text-xs font-mono text-ink-muted italic">null</span>
  }

  if (kind === 'boolean') {
    const v = Boolean(value)
    return (
      <button
        onClick={() => !readOnly && onChange(!v)}
        disabled={readOnly}
        className={cn(
          "px-2.5 py-0.5 rounded-md text-xs font-medium transition-colors",
          v ? "bg-emerald-500/15 text-emerald-500" : "bg-white/5 text-ink-muted",
          readOnly && "cursor-not-allowed opacity-60",
        )}
      >
        {v ? 'true' : 'false'}
      </button>
    )
  }

  if (kind === 'number') {
    return (
      <input
        type="number"
        value={value as number}
        onChange={(e) => {
          const n = Number(e.target.value)
          onChange(Number.isFinite(n) ? n : 0)
        }}
        readOnly={readOnly}
        className={cn(
          "w-28 px-2 py-1 rounded-md bg-white/5 border border-white/10 text-xs font-mono text-right",
          "focus:border-accent-lineage/50 focus:bg-white/8 outline-none transition-colors",
          readOnly && "cursor-not-allowed opacity-60",
        )}
      />
    )
  }

  // string
  const stringValue = value === null || value === undefined ? '' : String(value)
  const isMultiline = stringValue.includes('\n') || stringValue.length > 60

  if (isMultiline) {
    return (
      <textarea
        value={stringValue}
        onChange={(e) => onChange(e.target.value)}
        readOnly={readOnly}
        rows={Math.min(6, stringValue.split('\n').length + 1)}
        className={cn(
          "w-full max-w-[260px] px-2 py-1 rounded-md bg-white/5 border border-white/10 text-xs",
          "focus:border-accent-lineage/50 focus:bg-white/8 outline-none transition-colors resize-y",
          readOnly && "cursor-not-allowed opacity-60",
        )}
      />
    )
  }

  return (
    <input
      type="text"
      value={stringValue}
      onChange={(e) => onChange(e.target.value)}
      readOnly={readOnly}
      className={cn(
        "max-w-[260px] flex-1 px-2 py-1 rounded-md bg-white/5 border border-white/10 text-xs",
        "focus:border-accent-lineage/50 focus:bg-white/8 outline-none transition-colors",
        readOnly && "cursor-not-allowed opacity-60",
      )}
    />
  )
}

// ============================================
// TypeChip — popover to change a row's value kind
// ============================================

function TypeChip({
  kind,
  readOnly,
  onChange,
}: {
  kind: ValueKind
  readOnly?: boolean
  onChange?: (next: ValueKind) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const meta = KIND_META[kind]
  const Icon = meta.icon

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div className="relative flex-shrink-0" ref={ref}>
      <button
        onClick={() => !readOnly && onChange && setOpen(o => !o)}
        disabled={readOnly || !onChange}
        className={cn(
          "h-5 px-1.5 rounded text-2xs font-mono uppercase tracking-wider flex items-center gap-1",
          meta.tone,
          (readOnly || !onChange) ? "cursor-default" : "hover:brightness-125",
        )}
        title={readOnly ? `Type: ${meta.label}` : 'Change type'}
      >
        <Icon className="w-3 h-3" />
        {meta.label}
      </button>
      {open && onChange && (
        <div className="absolute left-0 top-7 z-50 min-w-[140px] rounded-lg border border-glass-border bg-canvas-elevated shadow-lg p-1">
          {(Object.keys(KIND_META) as ValueKind[]).map(k => {
            const km = KIND_META[k]
            const KIcon = km.icon
            return (
              <button
                key={k}
                onClick={() => { onChange(k); setOpen(false) }}
                className={cn(
                  "w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-xs hover:bg-white/10 transition-colors",
                  kind === k && "bg-white/5",
                )}
              >
                <KIcon className="w-3.5 h-3.5" />
                <span className="font-mono">{km.label}</span>
                {kind === k && <Check className="w-3 h-3 ml-auto text-accent-lineage" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ============================================
// AddRow — inline form for adding a key (object) or item (array)
// ============================================

function AddRow({
  onAdd,
  onCancel,
  existingKeys,
  variant,
}: {
  onAdd: (key: string, kind: ValueKind) => void
  onCancel: () => void
  existingKeys: string[]
  variant: 'object' | 'array'
}) {
  const [key, setKey] = useState('')
  const [kind, setKind] = useState<ValueKind>('string')

  const isDup = variant === 'object' && existingKeys.includes(key.trim())
  const canSubmit = variant === 'array' || (key.trim() !== '' && !isDup)

  const submit = () => {
    if (!canSubmit) return
    onAdd(variant === 'array' ? '' : key.trim(), kind)
    setKey('')
  }

  return (
    <div className="flex items-center gap-2 py-1.5 px-2 rounded-lg bg-white/[0.03] border border-accent-lineage/20">
      <TypeChip kind={kind} onChange={setKind} />
      {variant === 'object' && (
        <input
          autoFocus
          value={key}
          placeholder="key name"
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
            if (e.key === 'Escape') onCancel()
          }}
          className={cn(
            "flex-1 text-xs font-mono px-2 py-1 rounded bg-white/5 border outline-none",
            isDup ? "border-red-500/40" : "border-white/10 focus:border-accent-lineage/50",
          )}
        />
      )}
      {variant === 'array' && (
        <span className="flex-1 text-xs text-ink-muted">New {kind} item</span>
      )}
      <button
        onClick={submit}
        disabled={!canSubmit}
        className={cn(
          "w-6 h-6 flex items-center justify-center rounded transition-colors",
          canSubmit ? "text-emerald-500 hover:bg-emerald-500/10" : "text-ink-muted/40 cursor-not-allowed",
        )}
        title="Add"
      >
        <Check className="w-3.5 h-3.5" />
      </button>
      <button
        onClick={onCancel}
        className="w-6 h-6 flex items-center justify-center rounded text-ink-muted hover:bg-white/10"
        title="Cancel"
      >
        <XIcon className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}

// ============================================
// RawJsonEditor — fallback for deeply-nested subtrees
// ============================================

function RawJsonEditor({
  value,
  onChange,
  isOpen,
  onClose,
  inline,
}: {
  value: unknown
  onChange: (next: unknown) => void
  isOpen: boolean
  onClose: () => void
  inline?: boolean
}) {
  const [draft, setDraft] = useState(() => JSON.stringify(value, null, 2))
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (isOpen) {
      setDraft(JSON.stringify(value, null, 2))
      setError(null)
    }
  }, [isOpen, value])

  const commit = () => {
    try {
      const parsed = JSON.parse(draft)
      onChange(parsed)
      setError(null)
      if (!inline) onClose()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (!isOpen) return null

  const body = (
    <div className="space-y-2">
      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={inline ? 8 : 12}
        spellCheck={false}
        className={cn(
          "w-full px-3 py-2 rounded-md bg-black/30 border text-xs font-mono outline-none resize-y",
          error ? "border-red-500/40" : "border-white/10 focus:border-accent-lineage/50",
        )}
      />
      {error && (
        <div className="text-2xs text-red-500">Invalid JSON: {error}</div>
      )}
      <div className="flex items-center justify-end gap-2">
        {!inline && (
          <button
            onClick={onClose}
            className="px-3 py-1 rounded-md text-xs text-ink-muted hover:bg-white/10"
          >
            Cancel
          </button>
        )}
        <button
          onClick={commit}
          className="px-3 py-1 rounded-md text-xs bg-accent-lineage/20 text-accent-lineage hover:bg-accent-lineage/30"
        >
          Apply
        </button>
      </div>
    </div>
  )

  if (inline) {
    return <div className="rounded-lg border border-glass-border/40 bg-black/10 dark:bg-white/[0.02] p-3">{body}</div>
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-6"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.95, opacity: 0 }}
          onClick={(e) => e.stopPropagation()}
          className="w-full max-w-2xl rounded-xl border border-glass-border bg-canvas-elevated shadow-xl p-5"
        >
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-sm font-semibold text-ink">Edit as JSON</h4>
            <button onClick={onClose} className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10">
              <XIcon className="w-4 h-4 text-ink-muted" />
            </button>
          </div>
          {body}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}

export default PropertyEditor
