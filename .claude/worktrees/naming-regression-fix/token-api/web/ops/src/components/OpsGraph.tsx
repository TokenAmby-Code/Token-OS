// Bespoke directed-graph renderer for operational lineage. React Flow is the
// brief's eventual recommendation; for the cockpit pilot a self-contained SVG
// layered layout keeps the committed build lean while honoring the full
// `OpsGraph` contract: typed nodes, directional arrowheads, status-styled
// edges, click-to-inspect, type filters, and neighborhood focus.

import { useMemo, useState } from 'react';
import type { OpsGraph as OpsGraphData, OpsGraphNode, OpsGraphEdge } from '../types';
import { edgeVisual, nodeTypeColor } from '../modes';

const NODE_W = 150;
const NODE_H = 46;
const COL_GAP = 90;
const ROW_GAP = 22;
const MARGIN = 24;

type Placed = OpsGraphNode & { x: number; y: number; layer: number };

// Longest-path layering over directed edges (dagre-lite, left→right).
function layout(data: OpsGraphData): { nodes: Placed[]; w: number; h: number } {
  const byId = new Map(data.nodes.map((n) => [n.id, n]));
  const out = new Map<string, string[]>();
  const indeg = new Map<string, number>();
  data.nodes.forEach((n) => {
    out.set(n.id, []);
    indeg.set(n.id, 0);
  });
  data.edges.forEach((e) => {
    if (!e.directed || !byId.has(e.source) || !byId.has(e.target)) return;
    out.get(e.source)!.push(e.target);
    indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1);
  });

  // Kahn-style longest path → layer index.
  const layer = new Map<string, number>();
  const queue = data.nodes.filter((n) => (indeg.get(n.id) ?? 0) === 0).map((n) => n.id);
  queue.forEach((id) => layer.set(id, 0));
  const localIndeg = new Map(indeg);
  let head = 0;
  while (head < queue.length) {
    const id = queue[head++];
    const l = layer.get(id) ?? 0;
    for (const nxt of out.get(id) ?? []) {
      layer.set(nxt, Math.max(layer.get(nxt) ?? 0, l + 1));
      localIndeg.set(nxt, (localIndeg.get(nxt) ?? 1) - 1);
      if ((localIndeg.get(nxt) ?? 0) <= 0) queue.push(nxt);
    }
  }
  // Any node never reached (cycles / undirected-only) lands in layer 0.
  data.nodes.forEach((n) => { if (!layer.has(n.id)) layer.set(n.id, 0); });

  const cols = new Map<number, OpsGraphNode[]>();
  data.nodes.forEach((n) => {
    const l = layer.get(n.id) ?? 0;
    if (!cols.has(l)) cols.set(l, []);
    cols.get(l)!.push(n);
  });

  const maxRows = Math.max(...[...cols.values()].map((c) => c.length), 1);
  const placed: Placed[] = [];
  const colCount = Math.max(...[...cols.keys()]) + 1;
  for (let l = 0; l < colCount; l++) {
    const colNodes = cols.get(l) ?? [];
    const colH = colNodes.length * NODE_H + (colNodes.length - 1) * ROW_GAP;
    const fullH = maxRows * NODE_H + (maxRows - 1) * ROW_GAP;
    const yStart = MARGIN + (fullH - colH) / 2;
    colNodes.forEach((n, i) => {
      placed.push({
        ...n,
        layer: l,
        x: MARGIN + l * (NODE_W + COL_GAP),
        y: yStart + i * (NODE_H + ROW_GAP),
      });
    });
  }

  const w = MARGIN * 2 + colCount * NODE_W + (colCount - 1) * COL_GAP;
  const h = MARGIN * 2 + maxRows * NODE_H + (maxRows - 1) * ROW_GAP;
  return { nodes: placed, w, h };
}

type Props = { graph: OpsGraphData };

export function OpsGraph({ graph }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  const types = useMemo(
    () => Array.from(new Set(graph.nodes.map((n) => n.type))),
    [graph],
  );

  const visible = useMemo(
    () => ({
      nodes: graph.nodes.filter((n) => !hidden.has(n.type)),
      edges: graph.edges,
    }),
    [graph, hidden],
  );

  const { nodes, w, h } = useMemo(
    () => layout({ ...graph, nodes: visible.nodes }),
    [graph, visible.nodes],
  );
  const pos = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  // Neighborhood of the selected node (for focus dimming).
  const neighborhood = useMemo(() => {
    if (!selected) return null;
    const set = new Set<string>([selected]);
    graph.edges.forEach((e) => {
      if (e.source === selected) set.add(e.target);
      if (e.target === selected) set.add(e.source);
    });
    return set;
  }, [selected, graph]);

  const selectedNode = selected ? graph.nodes.find((n) => n.id === selected) ?? null : null;

  function toggleType(t: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  }

  return (
    <div className="graph">
      <div className="graph__filters">
        {types.map((t) => (
          <button
            key={t}
            className={`type-chip ${hidden.has(t) ? 'is-off' : ''}`}
            style={{ '--type-c': nodeTypeColor(t) } as React.CSSProperties}
            onClick={() => toggleType(t)}
          >
            <span className="type-chip__dot" />
            {t}
          </button>
        ))}
      </div>

      <div className="graph__stage">
        <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="graph__svg">
          <defs>
            <marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L7,3 L0,6 Z" fill="currentColor" />
            </marker>
          </defs>

          {visible.edges.map((e) => {
            const s = pos.get(e.source);
            const t = pos.get(e.target);
            if (!s || !t) return null;
            const ev = edgeVisual(e.status);
            const x1 = s.x + NODE_W;
            const y1 = s.y + NODE_H / 2;
            const x2 = t.x;
            const y2 = t.y + NODE_H / 2;
            const mx = (x1 + x2) / 2;
            const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
            const dim = neighborhood && !(neighborhood.has(e.source) && neighborhood.has(e.target));
            const dash = e.type === 'blocks' ? '2 5' : e.status === 'stale' ? '6 5' : undefined;
            return (
              <g key={e.id} style={{ color: ev.color, opacity: dim ? 0.12 : ev.opacity }}>
                <path
                  d={d}
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.5 + (e.weight ?? 0)}
                  strokeDasharray={dash}
                  markerEnd={e.directed ? 'url(#arrow)' : undefined}
                />
                {e.label ? (
                  <text x={mx} y={(y1 + y2) / 2 - 4} className="edge-label" textAnchor="middle">
                    {e.label}
                  </text>
                ) : null}
              </g>
            );
          })}

          {nodes.map((n) => {
            const dim = neighborhood && !neighborhood.has(n.id);
            const isSel = n.id === selected;
            return (
              <g
                key={n.id}
                transform={`translate(${n.x},${n.y})`}
                className={`gnode ${isSel ? 'is-sel' : ''}`}
                style={{ opacity: dim ? 0.2 : 1, cursor: 'pointer' }}
                onClick={() => setSelected(isSel ? null : n.id)}
              >
                <rect width={NODE_W} height={NODE_H} rx={5} className="gnode__box" />
                <rect width={4} height={NODE_H} rx={2} fill={nodeTypeColor(n.type)} />
                <text x={14} y={19} className="gnode__label">{truncate(n.label, 18)}</text>
                <text x={14} y={34} className="gnode__sub">{truncate(n.subtitle ?? n.type, 22)}</text>
                {n.status ? <circle cx={NODE_W - 12} cy={14} r={4} fill={statusDot(n.status)} /> : null}
              </g>
            );
          })}
        </svg>
      </div>

      {selectedNode ? (
        <aside className="inspector">
          <button className="inspector__close" onClick={() => setSelected(null)}>✕</button>
          <span className="inspector__type" style={{ color: nodeTypeColor(selectedNode.type) }}>
            {selectedNode.type}
          </span>
          <h3>{selectedNode.label}</h3>
          {selectedNode.subtitle ? <p className="inspector__sub">{selectedNode.subtitle}</p> : null}
          {selectedNode.status ? <p className="inspector__row"><span>status</span><strong>{selectedNode.status}</strong></p> : null}
          <EdgeList graph={graph} node={selectedNode} />
        </aside>
      ) : (
        <p className="graph__hint">Click a node to inspect its relations · click a type to filter</p>
      )}
    </div>
  );
}

function EdgeList({ graph, node }: { graph: OpsGraphData; node: OpsGraphNode }) {
  const related = graph.edges.filter((e) => e.source === node.id || e.target === node.id);
  if (!related.length) return null;
  const labelFor = (id: string) => graph.nodes.find((n) => n.id === id)?.label ?? id;
  return (
    <div className="inspector__edges">
      <span className="inspector__heading">relations</span>
      {related.map((e: OpsGraphEdge) => {
        const outgoing = e.source === node.id;
        const other = outgoing ? e.target : e.source;
        return (
          <div className="inspector__edge" key={e.id} style={{ color: edgeVisual(e.status).color }}>
            <span className="inspector__edge-dir">{outgoing ? '→' : '←'}</span>
            <span className="inspector__edge-type">{e.type}</span>
            <span className="inspector__edge-node">{labelFor(other)}</span>
          </div>
        );
      })}
    </div>
  );
}

function statusDot(status: string): string {
  switch (status.toLowerCase()) {
    case 'processing':
    case 'active':
    case 'completed':
      return 'var(--m-working)';
    case 'idle':
    case 'enabled':
      return 'var(--brass)';
    case 'stale':
      return 'var(--muted)';
    case 'blocked':
    case 'error':
      return 'var(--hazard)';
    default:
      return 'var(--line-bright)';
  }
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
