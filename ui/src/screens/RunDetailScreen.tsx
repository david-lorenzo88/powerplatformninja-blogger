import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { getRun, getWorkflows } from '../api/client'
import type { RunEvent, RunStatus } from '../api/types'
import { isTerminal } from '../api/types'
import { StatusBadge } from '../components/StatusBadge'
import { WorkflowCanvas } from '../components/WorkflowCanvas'
import { deriveNodes, latestActiveNode } from '../lib/deriveNodes'
import { parseMermaid } from '../lib/parseMermaid'
import { useRunStream } from '../hooks/useRunStream'
import { duration, formatTime } from '../lib/format'
import { RunResult } from '../components/RunResult'

export function RunDetailScreen() {
  const { id } = useParams<{ id: string }>()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tab, setTab] = useState<'node' | 'log'>('log')

  const { events, streamStatus, terminalStatus } = useRunStream(id)

  const run = useQuery({
    queryKey: ['run', id],
    queryFn: () => getRun(id!),
    enabled: !!id,
    // Poll for metadata/result until the run is terminal; then the stream's eof
    // has told us everything and we can stop.
    refetchInterval: (q) => (q.state.data && isTerminal(q.state.data.status) ? false : 3000),
  })

  const workflows = useQuery({
    queryKey: ['workflows'],
    queryFn: getWorkflows,
    staleTime: Infinity,
  })

  const graph = useMemo(() => {
    const wf = workflows.data?.find((w) => w.kind === run.data?.kind)
    return wf ? parseMermaid(wf.mermaid) : null
  }, [workflows.data, run.data?.kind])

  const nodeStates = useMemo(() => deriveNodes(events), [events])
  const latestId = useMemo(() => latestActiveNode(events), [events])

  const status: RunStatus | undefined = terminalStatus ?? run.data?.status
  const selectedEvents = selectedId
    ? events.filter((e) => e.executor_id === selectedId)
    : []

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="shrink-0 border-b border-slate-800 px-6 py-4">
        <Link to="/runs" className="text-xs text-slate-500 hover:text-accent">
          ← Runs
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold text-slate-100">
            {run.data?.label || 'Run'}
          </h1>
          {status && <StatusBadge status={status} />}
          <span className="font-mono text-xs text-slate-500">{run.data?.kind}</span>
          {run.data?.started_at && (
            <span className="text-xs text-slate-500">
              {isTerminal(status ?? 'running')
                ? `took ${duration(run.data.started_at, run.data.finished_at)}`
                : `running · ${duration(run.data.started_at, null)}`}
            </span>
          )}
          <StreamPill streamStatus={streamStatus} terminal={!!terminalStatus} />
        </div>
        {run.data && <RunResult run={run.data} />}
      </div>

      {/* Canvas + side panel */}
      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1">
          {graph ? (
            <WorkflowCanvas
              graph={graph}
              nodeStates={nodeStates}
              latestId={latestId}
              selectedId={selectedId}
              onSelect={(nid) => {
                setSelectedId(nid)
                setTab('node')
              }}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-slate-500">
              {run.data?.kind === 'cover'
                ? 'Cover runs have no agent graph.'
                : 'Loading workflow…'}
            </div>
          )}
        </div>

        <aside className="flex w-[26rem] shrink-0 flex-col border-l border-slate-800">
          <div className="flex shrink-0 gap-1 border-b border-slate-800 p-2">
            <TabButton active={tab === 'node'} onClick={() => setTab('node')}>
              {selectedId ? selectedId : 'Node'}
            </TabButton>
            <TabButton active={tab === 'log'} onClick={() => setTab('log')}>
              Log ({events.length})
            </TabButton>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {tab === 'node' ? (
              selectedId ? (
                <NodePanel executorId={selectedId} events={selectedEvents} />
              ) : (
                <p className="p-4 text-sm text-slate-500">Click an agent on the canvas.</p>
              )
            ) : (
              <EventList
                events={events.filter((e) => !isStreamChunk(e))}
                empty="Waiting for events…"
                showExecutor
              />
            )}
          </div>
        </aside>
      </div>
    </div>
  )
}

function StreamPill({
  streamStatus,
  terminal,
}: {
  streamStatus: 'connecting' | 'open' | 'closed'
  terminal: boolean
}) {
  if (terminal) return <span className="text-xs text-slate-600">stream closed</span>
  if (streamStatus === 'open')
    return (
      <span className="flex items-center gap-1.5 text-xs text-emerald-400">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" /> live
      </span>
    )
  return <span className="text-xs text-amber-400">connecting…</span>
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`max-w-[12rem] truncate rounded-md px-3 py-1.5 text-xs font-medium ${
        active ? 'bg-accent/15 text-accent' : 'text-slate-400 hover:text-slate-200'
      }`}
    >
      {children}
    </button>
  )
}

// A streamed-output chunk: the agent's text as it is produced. Shown in the
// node's Output block, not as individual log rows.
function isStreamChunk(e: RunEvent): boolean {
  return e.kind === 'log' && (e.data as { type?: string } | null)?.type === 'output'
}

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

function NodePanel({ executorId, events }: { executorId: string; events: RunEvent[] }) {
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
          <pre className="max-h-[45vh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
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

function EventList({
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
