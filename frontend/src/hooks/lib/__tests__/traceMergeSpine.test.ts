import { describe, expect, it } from 'vitest'
import { computeTraceMergeSpine } from '../traceMergeSpine'

const ce = (sourceUrn: string, targetUrn: string) => ({ sourceUrn, targetUrn })

describe('computeTraceMergeSpine', () => {
  it('returns empty when every participant is already known to the canvas', () => {
    const result = computeTraceMergeSpine({
      participantUrns: ['a', 'b'],
      containmentEdges: [ce('root', 'a'), ce('root', 'b')],
      knownAssignedUrns: new Set(['a', 'b']),
    })
    expect(result.spineUrns.size).toBe(0)
    expect(result.unreachableRoots.size).toBe(0)
  })

  it('collects intermediate ancestors until a known anchor is reached', () => {
    // Hierarchy: domain → schema → table → column. domain is on canvas.
    const result = computeTraceMergeSpine({
      participantUrns: ['column'],
      containmentEdges: [
        ce('domain', 'schema'),
        ce('schema', 'table'),
        ce('table', 'column'),
      ],
      knownAssignedUrns: new Set(['domain']),
    })
    expect(result.spineUrns).toEqual(new Set(['table', 'schema']))
    expect(result.unreachableRoots.size).toBe(0)
  })

  it('stops at the first known anchor — no over-collection', () => {
    // Hierarchy: alien-root → known-anchor → mid → leaf. Both known-anchor
    // and alien-root are above; the spine should stop at known-anchor.
    const result = computeTraceMergeSpine({
      participantUrns: ['leaf'],
      containmentEdges: [
        ce('alien-root', 'known-anchor'),
        ce('known-anchor', 'mid'),
        ce('mid', 'leaf'),
      ],
      knownAssignedUrns: new Set(['known-anchor']),
    })
    expect(result.spineUrns).toEqual(new Set(['mid']))
    expect(result.spineUrns.has('alien-root')).toBe(false)
    expect(result.unreachableRoots.size).toBe(0)
  })

  it('flags the topmost spine node as unreachable when no anchor is found', () => {
    const result = computeTraceMergeSpine({
      participantUrns: ['leaf'],
      containmentEdges: [
        ce('top', 'mid'),
        ce('mid', 'leaf'),
      ],
      knownAssignedUrns: new Set(),
    })
    expect(result.spineUrns).toEqual(new Set(['mid', 'top']))
    expect(result.unreachableRoots).toEqual(new Set(['top']))
  })

  it('flags the participant itself when it has no parent edge', () => {
    const result = computeTraceMergeSpine({
      participantUrns: ['orphan'],
      containmentEdges: [],
      knownAssignedUrns: new Set(),
    })
    expect(result.spineUrns.size).toBe(0)
    expect(result.unreachableRoots).toEqual(new Set(['orphan']))
  })

  it('shares spine ancestors across sibling participants', () => {
    // Two columns under the same table. Spine should include the table
    // exactly once and stop at the known schema anchor.
    const result = computeTraceMergeSpine({
      participantUrns: ['col-a', 'col-b'],
      containmentEdges: [
        ce('schema', 'table'),
        ce('table', 'col-a'),
        ce('table', 'col-b'),
      ],
      knownAssignedUrns: new Set(['schema']),
    })
    expect(result.spineUrns).toEqual(new Set(['table']))
    expect(result.unreachableRoots.size).toBe(0)
  })

  it('handles cycles in containment edges without infinite-looping', () => {
    const result = computeTraceMergeSpine({
      participantUrns: ['a'],
      containmentEdges: [
        ce('b', 'a'),
        ce('a', 'b'),  // cycle
      ],
      knownAssignedUrns: new Set(),
    })
    // Walk: a → parent=b → b's parent=a (already visited, stop).
    // Spine has {b}; unreachable since no anchor reached.
    expect(result.spineUrns).toEqual(new Set(['b']))
    expect(result.unreachableRoots).toEqual(new Set(['b']))
  })

  it('does not re-parent known canvas nodes (the Snowflake guardrail)', () => {
    // Known: REPORTING (a layer-rooted node on canvas). The trace returns
    // an ancestor "Snowflake" above REPORTING. A separate lineage column
    // also chains up to Snowflake. We expect Snowflake to enter the spine
    // (the column needs it as an anchor), but no containment edge to
    // REPORTING should be added — REPORTING stays in its existing layer.
    //
    // This test verifies the helper's spineUrns set; the caller is then
    // responsible for filtering containmentEdges so the target side is
    // never a `knownAssignedUrns` member.
    const result = computeTraceMergeSpine({
      participantUrns: ['far-column'],
      containmentEdges: [
        ce('snowflake', 'REPORTING'),  // reparent attempt — caller must drop
        ce('snowflake', 'far-table'),
        ce('far-table', 'far-column'),
      ],
      knownAssignedUrns: new Set(['REPORTING']),
    })
    expect(result.spineUrns.has('snowflake')).toBe(true)
    expect(result.spineUrns.has('far-table')).toBe(true)
    // The helper itself doesn't filter edges — that's the caller's job.
    // What it guarantees: REPORTING is NOT in the spine.
    expect(result.spineUrns.has('REPORTING')).toBe(false)
    expect(result.unreachableRoots).toEqual(new Set(['snowflake']))
  })
})
