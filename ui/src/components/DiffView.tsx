import { useState } from 'react'
import type { DeltaHunk, DeltaSectionChange } from '../api/types'

// The draft the crew wrote against the version that was published.
//
// This component renders; it never diffs. The hunks are computed once in
// `delta.py` and stored on the pair, so unlike the run canvas — where
// `deriveNodes.ts` has to be kept a faithful port of `derive_nodes()` — there is
// nothing here to keep in lockstep with the server.
//
// Side by side on a wide screen, stacked below it. At 375px two columns of prose
// are two columns of nothing, so the layout switches rather than shrinking.

const TONE: Record<DeltaHunk['op'], string> = {
  equal: 'border-slate-800 bg-slate-900/30',
  replace: 'border-amber-500/30 bg-amber-500/5',
  insert: 'border-emerald-500/30 bg-emerald-500/5',
  delete: 'border-rose-500/30 bg-rose-500/5',
}

const LABEL: Record<DeltaHunk['op'], string> = {
  equal: 'unchanged',
  replace: 'rewritten',
  insert: 'added by you',
  delete: 'cut by you',
}

export function SectionDiff({ sections }: { sections: DeltaSectionChange[] }) {
  const changed = sections.filter((s) => s.op !== 'equal')
  if (!changed.length) {
    return <p className="text-sm text-slate-500">The section structure survived unchanged.</p>
  }
  return (
    <ul className="space-y-1 text-sm">
      {changed.map((change, i) => (
        <li key={i} className="flex flex-wrap items-baseline gap-2">
          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-xs text-slate-400">
            {change.op === 'replace' ? 'renamed' : change.op === 'insert' ? 'added' : 'removed'}
          </span>
          {change.before && <span className="text-rose-300 line-through">{change.before}</span>}
          {change.before && change.after && <span className="text-slate-600">→</span>}
          {change.after && <span className="text-emerald-300">{change.after}</span>}
        </li>
      ))}
    </ul>
  )
}

export function DiffView({
  hunks,
  highlight,
}: {
  hunks: DeltaHunk[]
  /** A span from an observation, scrolled to when the author clicks it. */
  highlight?: string
}) {
  const [showUnchanged, setShowUnchanged] = useState(false)
  const visible = showUnchanged ? hunks : hunks.filter((h) => h.op !== 'equal')
  const unchanged = hunks.length - hunks.filter((h) => h.op !== 'equal').length

  if (!hunks.length) {
    return <p className="text-sm text-slate-500">Nothing to compare yet.</p>
  }

  return (
    <div className="space-y-3">
      {unchanged > 0 && (
        <button
          type="button"
          onClick={() => setShowUnchanged((v) => !v)}
          className="min-h-11 text-sm text-slate-400 hover:text-slate-200 active:text-slate-200 lg:min-h-0"
        >
          {showUnchanged ? 'Hide' : 'Show'} {unchanged} unchanged block
          {unchanged === 1 ? '' : 's'}
        </button>
      )}
      {visible.map((hunk, i) => {
        const isHit = Boolean(highlight) && (hunk.before.includes(highlight!) || hunk.after.includes(highlight!))
        return (
          <div
            key={i}
            className={`rounded-lg border p-3 ${TONE[hunk.op]} ${isHit ? 'ring-2 ring-accent' : ''}`}
          >
            <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span className="font-medium text-slate-400">{LABEL[hunk.op]}</span>
              {hunk.section && <span>· {hunk.section}</span>}
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              {hunk.before && (
                <pre className="overflow-x-auto whitespace-pre-wrap break-words font-sans text-sm text-rose-200/90">
                  {hunk.before}
                </pre>
              )}
              {hunk.after && (
                <pre className="overflow-x-auto whitespace-pre-wrap break-words font-sans text-sm text-emerald-200/90">
                  {hunk.after}
                </pre>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
