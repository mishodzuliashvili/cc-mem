import { useEffect, useRef } from 'react'
import { Network, DataSet } from 'vis-network/standalone'
import { api } from '../api'

const COLORS = { global: '#1f6feb', project: '#a371f7' }
const POS_KEY = 'ccmem.graph.positions'

const toNode = (n) => ({
  id: n.id,
  label: n.label || `#${n.id}`,
  title: n.summary || '',
  value: 12 + Math.min(28, (n.access_count || 0) * 3 + (n.importance || 1) * 4),
  color: { background: COLORS[n.scope] || '#39414d', border: '#0e1116' },
  font: { color: '#d6deeb', size: 14 },
})
const edgeId = (e) => `${e.src}-${e.dst}-${e.kind}`
const toEdge = (e) => ({
  id: edgeId(e),
  from: e.src,
  to: e.dst,
  value: e.weight,
  title: `${e.kind} · w=${e.weight.toFixed(2)}`,
  color: { color: '#39414d', highlight: '#7ee787' },
})

const loadPos = () => {
  try { return JSON.parse(localStorage.getItem(POS_KEY)) || {} } catch { return {} }
}

export default function GraphView({ onOpen, tick }) {
  const elRef = useRef(null)
  const netRef = useRef(null)
  const nodesRef = useRef(null)
  const edgesRef = useRef(null)
  const posRef = useRef(loadPos()) // { id: {x, y} } persisted across refreshes

  // Create the network once.
  useEffect(() => {
    nodesRef.current = new DataSet([])
    edgesRef.current = new DataSet([])
    netRef.current = new Network(
      elRef.current,
      { nodes: nodesRef.current, edges: edgesRef.current },
      {
        nodes: { shape: 'dot', scaling: { min: 10, max: 42 } },
        edges: { scaling: { min: 1, max: 8 }, smooth: { type: 'continuous' } },
        physics: {
          stabilization: true,
          barnesHut: { gravitationalConstant: -9000, springLength: 140 },
        },
        interaction: { hover: true, tooltipDelay: 120 },
      },
    )
    netRef.current.on('click', (p) => { if (p.nodes.length) onOpen(p.nodes[0]) })
    // Save positions only on settle + after a drag — never on a timer.
    const save = () => {
      try {
        posRef.current = { ...posRef.current, ...netRef.current.getPositions() }
        localStorage.setItem(POS_KEY, JSON.stringify(posRef.current))
      } catch { /* ignore quota/serialization errors */ }
    }
    netRef.current.on('stabilized', save)
    netRef.current.on('dragEnd', save)
    return () => { netRef.current?.destroy(); netRef.current = null }
  }, [onOpen])

  // Sync data on change — and seed each node at its remembered position so a
  // refresh starts from the same layout instead of re-randomizing.
  useEffect(() => {
    let alive = true
    api.graph().then((g) => {
      if (!alive || !nodesRef.current) return
      const nodes = nodesRef.current
      const edges = edgesRef.current
      const nIds = new Set(g.nodes.map((n) => n.id))
      const eIds = new Set(g.edges.map(edgeId))
      nodes.update(g.nodes.map((n) => {
        const pos = posRef.current[n.id]
        return pos ? { ...toNode(n), x: pos.x, y: pos.y } : toNode(n)
      }))
      edges.update(g.edges.map(toEdge))
      nodes.getIds().forEach((id) => { if (!nIds.has(id)) nodes.remove(id) })
      edges.getIds().forEach((id) => { if (!eIds.has(id)) edges.remove(id) })
    })
    return () => { alive = false }
  }, [tick])

  const resetLayout = () => {
    posRef.current = {}
    localStorage.removeItem(POS_KEY)
    const fresh = nodesRef.current.get().map(({ x, y, ...rest }) => rest)
    nodesRef.current.clear()
    nodesRef.current.add(fresh)
    netRef.current.setOptions({ physics: { enabled: true } })
    netRef.current.stabilize()
  }

  return (
    <>
      <div className="legend">
        <span className="pill global">global</span>
        <span className="pill project">project</span>
        node size = importance + access · edge width = weight · drag to arrange
        (positions are remembered) · click a node to open
        <button className="btn ghost" style={{ marginLeft: 10 }} onClick={resetLayout}>
          Reset layout
        </button>
      </div>
      <div id="graph" ref={elRef} />
    </>
  )
}
