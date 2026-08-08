// The canvas, for a screen too narrow to draw one.
//
// At 390px the React Flow graph is unreadable even when it is given the whole
// width — a four-wide rank forces fitView to about 0.3, which renders a 14px
// label at four. So on a phone the same run is a list.
//
// It is fed by exactly the two things the canvas is fed by: parseMermaid() for
// topology and labels, deriveNodes() for state. That is deliberate. deriveNodes
// is a faithful port of derive_nodes() in server/api.py, and the invariant that
// makes a replayed run animate identically to a live one only holds while every
// renderer folds the same event log the same way. This is a third *consumer* of
// that fold, never a second fold — if you add a field here, add it to AgentNode
// too rather than deriving anything new.
//
// Ordering comes from graph.nodes as parsed. WorkflowViz emits every node
// declaration before any edge, in pipeline order, and parseMermaid builds a Map,
// which preserves insertion order — so the list reads top-to-bottom the way the
// crew actually runs, without pulling dagre onto a phone to find out.

import type { NodeState } from '../api/types'
import { duration } from '../lib/format'
import type { ParsedGraph } from '../lib/parseMermaid'

export function RunStepList({
  graph,
  nodeStates,
  latestId,
  selectedId,
  live,
  onSelect,
}: {
  graph: ParsedGraph
  nodeStates: Record<string, NodeState>
  latestId: string | null
  selectedId: string | null
  /** Whether the run is still going. Only a live run has a node that is still
   *  accumulating time; on a finished one every node has a real elapsed span. */
  live: boolean
  onSelect: (id: string) => void
}) {
  // Fan-ins are synthetic join points, not agents — the canvas already makes
  // them unselectable, and in a list they are just noise.
  const steps = graph.nodes.filter((n) => !n.isFanIn)

  return (
    <ol className="divide-y divide-slate-800/60">
      {steps.map((node) => {
        const state = nodeStates[node.id]
        const active = state?.status === 'active'
        // deriveNodes has no per-node "done" — the terminal state is the run's.
        // So "currently running" is the latest node *of a run that is still
        // going*; without the `live` guard a finished run's last node measures
        // time-since-it-ran and reads as hundreds of hours.
        const running = live && active && latestId === node.id
        return (
          <li key={node.id}>
            <button
              onClick={() => onSelect(node.id)}
              disabled={!state}
              className={`flex min-h-14 w-full items-center gap-3 px-4 py-3 text-left transition-colors disabled:opacity-40 ${
                selectedId === node.id ? 'bg-accent/10' : 'active:bg-slate-900/60'
              }`}
            >
              <span
                className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                  active ? `bg-accent ${running ? 'animate-pulse' : ''}` : 'bg-slate-700'
                }`}
              />
              <span className="min-w-0 flex-1">
                <span
                  className={`block truncate text-sm font-medium ${
                    active ? 'text-slate-100' : 'text-slate-400'
                  }`}
                >
                  {node.label}
                </span>
                <span className="mt-0.5 block text-[11px] text-slate-500">
                  {state ? (
                    <>
                      {state.events} event{state.events === 1 ? '' : 's'}
                      {state.logs > 0 && ` · ${state.logs} log${state.logs === 1 ? '' : 's'}`}
                      {' · '}
                      {/* A null end makes duration() measure to now, which is
                          what keeps the node the crew is inside ticking. */}
                      {duration(state.first_seen, running ? null : state.last_seen)}
                    </>
                  ) : (
                    'pending'
                  )}
                </span>
              </span>
              {node.isStart && (
                <span className="shrink-0 rounded bg-slate-800 px-1 text-[9px] uppercase tracking-wide text-slate-400">
                  start
                </span>
              )}
              {state && <span className="shrink-0 text-slate-600">›</span>}
            </button>
          </li>
        )
      })}
    </ol>
  )
}
