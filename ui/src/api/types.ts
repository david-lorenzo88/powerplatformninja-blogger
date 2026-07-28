// Mirrors the server contract in docs/ARCHITECTURE.md and src/ppn_blogger/server.
// Kept in one place so every screen shares one source of truth for shapes.

export type RunKind = 'suggest' | 'write' | 'cover'

export type RunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export const TERMINAL: RunStatus[] = ['succeeded', 'failed', 'cancelled', 'interrupted']

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL.includes(status)
}

export interface Health {
  ok: boolean
  language: string
  concurrency: number
  cover: { enabled: boolean; model: string; route: string; configured: boolean }
  wordpress: { configured: boolean; url: string }
  search: { provider: string; configured: boolean }
  translation: { enabled: boolean }
}

export interface Run {
  id: string
  kind: RunKind
  status: RunStatus
  label: string
  params: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string | null
  config_version: string | null
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
}

// Per-executor status the server derives from the event log (derive_nodes).
// The client re-derives the same shape so canvas and log can never disagree.
export interface NodeState {
  id: string
  events: number
  logs: number
  first_seen: string
  last_seen: string
  status: 'pending' | 'active'
}

export interface RunDetail extends Run {
  nodes: Record<string, NodeState>
  event_count: number
}

export type EventKind = 'status' | 'node' | 'log' | 'eof'

// One row of the run event log (LiveEvent.to_dict), or the synthetic eof frame.
export interface RunEvent {
  seq: number
  kind: EventKind
  executor_id: string
  level: string
  message: string
  data: Record<string, unknown> | null
  ts: string
}

export interface WorkflowGraph {
  kind: RunKind
  title: string
  mermaid: string
  nodes: string[]
}

export interface ConfigListItem {
  name: string
  format: 'yaml' | 'markdown' | string
  version: number
  note: string
  updated_at: string
}

export interface ConfigContent {
  name: string
  format: string
  version: number
  content: string
  updated_at: string
}

export interface ConfigHistoryItem {
  version: number
  note: string
  created_at: string
  size: number
}

export interface ConfigVersion {
  name: string
  version: number
  content: string
  note: string
}

export interface DraftListItem {
  file: string
  title?: string
  slug?: string
  language?: string
  translation_of?: string
  category?: string
  tags?: string[]
  word_count?: number
  read_minutes?: number
  generated?: string
  review?: { approved: boolean; score: number; blockers: number }
  has_review?: boolean
  has_cover?: boolean
  [key: string]: unknown
}

export interface Draft {
  file: string
  front_matter: Record<string, unknown>
  markdown: string
  review: string
}

// Request bodies.
export interface SuggestRequest {
  instruction?: string
  label?: string
}

export interface Topic {
  title: string
  [key: string]: unknown
}

export interface WriteRequest {
  topic: Topic
  push?: boolean | null
  cover?: boolean | null
  translate?: boolean | null
  label?: string
}
