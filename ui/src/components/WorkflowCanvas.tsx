import { useMemo } from 'react'
import { Background, BackgroundVariant, Controls, MarkerType, ReactFlow } from '@xyflow/react'
import type { Edge, Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { NodeState } from '../api/types'
import type { ParsedGraph } from '../lib/parseMermaid'
import { layoutGraph } from '../lib/layout'
import { AgentNode } from './AgentNode'
import type { AgentNodeData } from './AgentNode'

const nodeTypes = { agent: AgentNode }

export function WorkflowCanvas({
  graph,
  nodeStates,
  latestId,
  selectedId,
  onSelect,
}: {
  graph: ParsedGraph
  nodeStates: Record<string, NodeState>
  latestId: string | null
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  // Layout is a pure function of topology — compute once per graph, never on a
  // status change, so live events don't shuffle the canvas.
  const layout = useMemo(() => layoutGraph(graph), [graph])

  const nodes: Node[] = useMemo(
    () =>
      graph.nodes.map((n) => {
        const state = nodeStates[n.id]
        const data: AgentNodeData = {
          label: n.label,
          isStart: n.isStart,
          isFanIn: n.isFanIn,
          status: state?.status ?? 'pending',
          latest: latestId === n.id,
          selected: selectedId === n.id,
          events: state?.events ?? 0,
          logs: state?.logs ?? 0,
        }
        return {
          id: n.id,
          type: 'agent',
          position: layout.positions.get(n.id) ?? { x: 0, y: 0 },
          data,
          selectable: !n.isFanIn,
        }
      }),
    [graph, layout, nodeStates, latestId, selectedId],
  )

  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((e) => {
        const hot = nodeStates[e.source]?.status === 'active'
        return {
          id: `${e.source}->${e.target}`,
          source: e.source,
          target: e.target,
          label: e.label,
          animated: hot,
          style: { stroke: hot ? '#c084fc' : '#334155', strokeWidth: hot ? 2 : 1.5 },
          markerEnd: { type: MarkerType.ArrowClosed, color: hot ? '#c084fc' : '#334155' },
        }
      }),
    [graph, nodeStates],
  )

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodeClick={(_, node) => onSelect(node.id)}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      minZoom={0.2}
      nodesDraggable={false}
      nodesConnectable={false}
      proOptions={{ hideAttribution: true }}
      className="bg-slate-950"
    >
      <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#1e293b" />
      <Controls showInteractive={false} className="!border-slate-700 !bg-slate-900" />
    </ReactFlow>
  )
}
