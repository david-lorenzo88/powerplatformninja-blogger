import type { RunEvent } from '../api/types'

// A streamed-output chunk: the agent's text as it is produced. It belongs in the
// node's Output block, reassembled, not as one log row per token — which is why
// every event list in the app filters these out first.
//
// Deliberately not in deriveNodes.ts: that file is a line-for-line port of
// derive_nodes() in server/api.py and stays that way.
export function isStreamChunk(e: RunEvent): boolean {
  return e.kind === 'log' && (e.data as { type?: string } | null)?.type === 'output'
}
