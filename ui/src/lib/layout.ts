import dagre from '@dagrejs/dagre'
import type { ParsedGraph } from './parseMermaid'

export const NODE_W = 184
export const NODE_H = 48
export const FANIN = 26

export interface Layout {
  positions: Map<string, { x: number; y: number }>
  width: number
  height: number
}

// Layered top-down layout via dagre. Both workflow graphs are cyclic (the source
// and revision loops); dagre handles back-edges by ranking on a spanning tree,
// which is exactly the readable "flow down, loop back up" we want. Computed once
// per graph and memoised by the caller — live status never re-runs layout.
export function layoutGraph(graph: ParsedGraph): Layout {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 36, ranksep: 64, marginx: 24, marginy: 24 })
  g.setDefaultEdgeLabel(() => ({}))

  for (const node of graph.nodes) {
    const size = node.isFanIn ? FANIN : NODE_H
    g.setNode(node.id, { width: node.isFanIn ? FANIN : NODE_W, height: size })
  }
  for (const edge of graph.edges) {
    g.setEdge(edge.source, edge.target)
  }

  dagre.layout(g)

  const positions = new Map<string, { x: number; y: number }>()
  for (const node of graph.nodes) {
    const n = g.node(node.id)
    if (!n) continue
    // dagre gives node centres; React Flow positions from the top-left.
    positions.set(node.id, { x: n.x - n.width / 2, y: n.y - n.height / 2 })
  }
  const { width = 0, height = 0 } = g.graph()
  return { positions, width, height }
}
