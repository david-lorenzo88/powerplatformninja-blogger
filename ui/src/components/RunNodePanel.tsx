// One executor's output and activity, and the raw event list.
//
// Extracted from RunDetailScreen so the desktop side panel and the mobile sheet
// render the identical component. Forking these would mean two places to change
// when an event kind is added, and two chances for the phone to disagree with
// the desktop about what a run did.

import type { RunEvent } from '../api/types'
import { formatTime } from '../lib/format'
import { isStreamChunk } from '../lib/runEvents'

// Agent outputs are JSON; pretty-print when we can, otherwise show as-is.
function formatOutput(text: string): string {
  const t = text.trim()
  if (t.startsWith('{') || t.startsWith('[')) {
    try {
      return JSON.stringify(JSON.parse(t), null, 2)
    } catch {
      // Partial JSON while streaming — show the raw text.
    }
  }
  return text
}

export function NodePanel({ executorId, events }: { executorId: string; events: RunEvent[] }) {
  // Prefer the clean output the executor_completed event carries; fall back to
  // the streamed text (which reconstructs the full output for agents that stream
  // token-by-token). The completed event is also our signal that streaming ended.
  const completed = [...events]
    .reverse()
    .find(
      (e) =>
        e.kind === 'node' &&
        (e.data as { type?: string } | null)?.type === 'executor_completed',
    )
  const finalOutput = (completed?.data as { output?: string } | null)?.output
  const streamed = events
    .filter(isStreamChunk)
    .map((e) => e.message)
    .join('')
  const output = finalOutput || streamed
  const activity = events.filter((e) => !isStreamChunk(e))
  const streaming = !completed && streamed.length > 0

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 p-3">
        <div className="mb-1 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
          Output
          {streaming && (
            <span className="animate-pulse font-normal normal-case text-accent">streaming…</span>
          )}
        </div>
        {output ? (
          <pre className="max-h-[45dvh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
            {formatOutput(output)}
          </pre>
        ) : (
          <p className="text-xs text-slate-500">
            {activity.length ? 'Waiting for output…' : `No activity from ${executorId} yet.`}
          </p>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <EventList events={activity} empty="No activity yet." />
      </div>
    </div>
  )
}

export function EventList({
  events,
  empty,
  showExecutor,
}: {
  events: RunEvent[]
  empty: string
  showExecutor?: boolean
}) {
  if (events.length === 0) {
    return <p className="p-4 text-sm text-slate-500">{empty}</p>
  }
  // Keep the DOM bounded on runs that emit thousands of events; the newest are
  // the ones worth watching. Never drop silently — say how many are hidden.
  const MAX_ROWS = 600
  const hidden = Math.max(0, events.length - MAX_ROWS)
  const shown = hidden > 0 ? events.slice(-MAX_ROWS) : events
  return (
    <ul className="divide-y divide-slate-800/60 font-mono text-xs">
      {hidden > 0 && (
        <li className="bg-slate-900/40 px-3 py-1.5 text-[10px] text-slate-500">
          {hidden} earlier event{hidden === 1 ? '' : 's'} hidden · showing the latest {MAX_ROWS}
        </li>
      )}
      {shown.map((e) => (
        <li key={e.seq} className="px-3 py-1.5">
          <div className="flex items-center gap-2 text-[10px] text-slate-500">
            <span>{formatTime(e.ts).split(', ')[1] ?? formatTime(e.ts)}</span>
            <KindChip kind={e.kind} level={e.level} />
            {showExecutor && e.executor_id && (
              <span className="text-accent/70">{e.executor_id}</span>
            )}
          </div>
          <div
            className={`mt-0.5 break-words ${
              e.level === 'error' ? 'text-rose-300' : 'text-slate-300'
            }`}
          >
            {e.message}
          </div>
        </li>
      ))}
    </ul>
  )
}

function KindChip({ kind, level }: { kind: string; level: string }) {
  const color =
    level === 'error'
      ? 'bg-rose-500/20 text-rose-300'
      : kind === 'node'
        ? 'bg-accent/20 text-accent'
        : kind === 'status'
          ? 'bg-emerald-500/20 text-emerald-300'
          : 'bg-slate-700/50 text-slate-400'
  return <span className={`rounded px-1 ${color}`}>{kind}</span>
}
