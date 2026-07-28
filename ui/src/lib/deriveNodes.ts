import type { NodeState, RunEvent } from '../api/types'

// Faithful port of derive_nodes() in src/ppn_blogger/server/api.py. Folding the
// event log the SAME way client-side is what guarantees the canvas and the
// transcript can never disagree, and that a replayed run animates exactly like a
// live one. Keep this in lockstep with the Python:
//
//   - only events with a truthy executor_id contribute
//   - any executor that has produced an event is "active"; all others "pending"
//     (there is deliberately no per-node "done" — the terminal state is the
//     run-level status)
//   - `logs` counts events of kind "log"
export function deriveNodes(events: RunEvent[]): Record<string, NodeState> {
  const nodes: Record<string, NodeState> = {}
  for (const event of events) {
    const executorId = event.executor_id
    if (!executorId) continue
    let node = nodes[executorId]
    if (!node) {
      node = {
        id: executorId,
        events: 0,
        logs: 0,
        first_seen: event.ts,
        last_seen: event.ts,
        status: 'pending',
      }
      nodes[executorId] = node
    }
    node.events += 1
    node.last_seen = event.ts
    node.status = 'active'
    if (event.kind === 'log') node.logs += 1
  }
  return nodes
}

// The executor of the highest-seq node event — the agent that fired most
// recently. Derived from the same log; used only to accent the current node,
// never to change a node's active/pending status.
export function latestActiveNode(events: RunEvent[]): string | null {
  let latest: string | null = null
  let bestSeq = -1
  for (const event of events) {
    if (event.kind === 'node' && event.executor_id && event.seq > bestSeq) {
      bestSeq = event.seq
      latest = event.executor_id
    }
  }
  return latest
}
