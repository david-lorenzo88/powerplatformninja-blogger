// Parse the `flowchart TD` string from GET /api/workflows into nodes and edges.
// The topology is the real workflow object rendered by WorkflowViz, so parsing
// it (rather than hand-listing nodes) keeps the canvas from ever drifting from
// the code. Shapes seen in practice:
//   scout_dispatcher["scout_dispatcher (Start)"];      rectangular executor
//   fan_in__review_gate__7a2c1801((fan-in))            synthetic fan-in circle
//   docs_scout --> fan_in__review_gate__7a2c1801;       edge

export interface GraphNode {
  id: string
  label: string
  isStart: boolean
  isFanIn: boolean
}

export interface GraphEdge {
  source: string
  target: string
  label?: string
}

export interface ParsedGraph {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

const RECT = /^([A-Za-z0-9_]+)\["(.*)"\];?$/
const CIRCLE = /^([A-Za-z0-9_]+)\(\((.*)\)\);?$/
const EDGE = /^([A-Za-z0-9_]+)\s*-->\s*(?:\|(.*?)\|\s*)?([A-Za-z0-9_]+);?$/

export function parseMermaid(mermaid: string): ParsedGraph {
  const nodes = new Map<string, GraphNode>()
  const edges: GraphEdge[] = []

  const ensure = (id: string): GraphNode => {
    let n = nodes.get(id)
    if (!n) {
      n = { id, label: id, isStart: false, isFanIn: false }
      nodes.set(id, n)
    }
    return n
  }

  for (const raw of mermaid.split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('flowchart') || line.startsWith('%%')) continue

    const edge = EDGE.exec(line)
    if (edge) {
      const [, source, label, target] = edge
      ensure(source)
      ensure(target)
      edges.push({ source, target, label: label || undefined })
      continue
    }

    const circle = CIRCLE.exec(line)
    if (circle) {
      const [, id, label] = circle
      const n = ensure(id)
      n.label = label
      n.isFanIn = true
      continue
    }

    const rect = RECT.exec(line)
    if (rect) {
      const [, id, label] = rect
      const n = ensure(id)
      // Strip the " (Start)" marker WorkflowViz adds to the entry node.
      n.isStart = / \(Start\)\s*$/.test(label)
      n.label = label.replace(/ \(Start\)\s*$/, '')
      continue
    }
  }

  return { nodes: [...nodes.values()], edges }
}
