import { Handle, Position } from '@xyflow/react'
import type { NodeProps } from '@xyflow/react'

// Data React Flow carries for each node. Status is folded from the event log
// (pending | active); `latest` accents the agent that fired most recently.
export interface AgentNodeData {
  label: string
  isStart: boolean
  isFanIn: boolean
  status: 'pending' | 'active'
  latest: boolean
  selected: boolean
  events: number
  logs: number
  [key: string]: unknown
}

export function AgentNode({ data }: NodeProps) {
  const d = data as AgentNodeData

  if (d.isFanIn) {
    const active = d.status === 'active'
    return (
      <div
        className={`flex h-[26px] w-[26px] rotate-45 items-center justify-center rounded-[6px] border ${
          active ? 'border-accent bg-accent/30' : 'border-slate-700 bg-slate-800'
        }`}
        title="fan-in"
      >
        <Handle type="target" position={Position.Top} className="!bg-slate-500" />
        <Handle type="source" position={Position.Bottom} className="!bg-slate-500" />
      </div>
    )
  }

  const active = d.status === 'active'
  const ring = d.selected
    ? 'ring-2 ring-accent'
    : d.latest && active
      ? 'ring-2 ring-accent/60'
      : ''
  const body = active
    ? 'border-accent/70 bg-accent/10 text-slate-100'
    : 'border-slate-700 bg-slate-900 text-slate-400'

  return (
    <div
      className={`relative w-[184px] rounded-lg border px-3 py-2 shadow-sm transition-colors ${body} ${ring}`}
    >
      <Handle type="target" position={Position.Top} className="!bg-slate-600" />
      <div className="flex items-center gap-2">
        {active && (
          <span
            className={`h-2 w-2 shrink-0 rounded-full bg-accent ${d.latest ? 'animate-pulse' : ''}`}
          />
        )}
        {d.isStart && !active && <span className="h-2 w-2 shrink-0 rounded-full bg-slate-600" />}
        <span className="truncate text-sm font-medium">{d.label}</span>
      </div>
      {active && (d.events > 0 || d.logs > 0) && (
        <div className="mt-0.5 text-[10px] text-slate-500">
          {d.events} event{d.events === 1 ? '' : 's'}
          {d.logs > 0 && ` · ${d.logs} log${d.logs === 1 ? '' : 's'}`}
        </div>
      )}
      {d.isStart && (
        <span className="absolute -top-2 left-2 rounded bg-slate-800 px-1 text-[9px] uppercase tracking-wide text-slate-400">
          start
        </span>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-slate-600" />
    </div>
  )
}
