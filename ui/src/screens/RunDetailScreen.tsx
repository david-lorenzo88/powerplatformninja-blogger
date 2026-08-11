import { Suspense, lazy, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { getRun, getWorkflows } from '../api/client'
import type { RunStatus } from '../api/types'
import { isTerminal } from '../api/types'
import { StatusBadge } from '../components/StatusBadge'
import { Modal } from '../components/Modal'
import { EventList, NodePanel } from '../components/RunNodePanel'
import { RunStepList } from '../components/RunStepList'
import { deriveNodes, latestActiveNode } from '../lib/deriveNodes'
import { parseMermaid } from '../lib/parseMermaid'
import { isStreamChunk } from '../lib/runEvents'
import { useIsDesktop } from '../hooks/useMediaQuery'
import { useRunStream } from '../hooks/useRunStream'
import { duration } from '../lib/format'
import { RunCost } from '../components/RunCost'
import { RunResult } from '../components/RunResult'

// React Flow and dagre are ~400KB and only the desktop layout renders them, so
// they load when the canvas first mounts rather than with the route. A phone
// showing the step list never fetches them at all — which `hidden lg:block`
// could not achieve, since a hidden component still mounts.
const WorkflowCanvas = lazy(() =>
  import('../components/WorkflowCanvas').then((m) => ({ default: m.WorkflowCanvas })),
)

export function RunDetailScreen() {
  const { id } = useParams<{ id: string }>()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tab, setTab] = useState<'node' | 'log' | 'cost'>('log')
  const [mobileTab, setMobileTab] = useState<'steps' | 'log' | 'cost'>('steps')
  const isDesktop = useIsDesktop()

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
  const noGraphReason =
    run.data?.kind === 'cover' ? 'Cover runs have no agent graph.' : 'Loading workflow…'

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <Link to="/runs" className="inline-flex min-h-8 items-center text-xs text-slate-500 hover:text-accent">
          ← Runs
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="text-lg font-semibold text-slate-100">{run.data?.label || 'Run'}</h1>
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

      {isDesktop ? (
        /* Canvas ‖ side panel */
        <div className="flex min-h-0 flex-1">
          <div className="min-w-0 flex-1">
            {graph ? (
              <Suspense
                fallback={
                  <div className="flex h-full items-center justify-center text-sm text-slate-500">
                    loading graph…
                  </div>
                }
              >
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
              </Suspense>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-slate-500">
                {noGraphReason}
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
              <TabButton active={tab === 'cost'} onClick={() => setTab('cost')}>
                Cost
              </TabButton>
            </div>
            <div className="min-h-0 flex-1 overflow-auto">
              {tab === 'cost' ? (
                <div className="p-4">{run.data && <RunCost run={run.data} />}</div>
              ) : tab === 'node' ? (
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
      ) : (
        /* Steps ‖ Log, with the node detail as a sheet */
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex shrink-0 gap-1 border-b border-slate-800 p-2">
            <TabButton active={mobileTab === 'steps'} onClick={() => setMobileTab('steps')}>
              Steps ({Object.keys(nodeStates).length}/
              {graph?.nodes.filter((n) => !n.isFanIn).length ?? 0})
            </TabButton>
            <TabButton active={mobileTab === 'cost'} onClick={() => setMobileTab('cost')}>
              Cost
            </TabButton>
            <TabButton active={mobileTab === 'log'} onClick={() => setMobileTab('log')}>
              Log ({events.length})
            </TabButton>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {mobileTab === 'cost' ? (
              <div className="p-4">{run.data && <RunCost run={run.data} />}</div>
            ) : mobileTab === 'steps' ? (
              graph ? (
                <RunStepList
                  graph={graph}
                  nodeStates={nodeStates}
                  latestId={latestId}
                  selectedId={selectedId}
                  live={!!status && !isTerminal(status)}
                  onSelect={setSelectedId}
                />
              ) : (
                <p className="p-4 text-sm text-slate-500">{noGraphReason}</p>
              )
            ) : (
              <EventList
                events={events.filter((e) => !isStreamChunk(e))}
                empty="Waiting for events…"
                showExecutor
              />
            )}
          </div>

          {selectedId && (
            <Modal title={selectedId} onClose={() => setSelectedId(null)} width="max-w-2xl">
              <NodePanel executorId={selectedId} events={selectedEvents} />
            </Modal>
          )}
        </div>
      )}
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
      className={`min-h-11 max-w-[12rem] truncate rounded-md px-3 text-xs font-medium lg:min-h-0 lg:py-1.5 ${
        active ? 'bg-accent/15 text-accent' : 'text-slate-400 hover:text-slate-200 active:text-slate-200'
      }`}
    >
      {children}
    </button>
  )
}
