import { useEffect, useMemo, useRef, useState } from "react";
import {
  forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide,
  Simulation, SimulationNodeDatum, SimulationLinkDatum,
} from "d3-force";
import { select } from "d3-selection";
import { zoom, ZoomBehavior, zoomIdentity } from "d3-zoom";
import { drag } from "d3-drag";

import { api } from "../api";
import { useStore } from "../store";
import type { GraphNode, GraphEdge } from "../types";

// ── Visual encoding ────────────────────────────────────────────────────────

const STALENESS_COLOR: Record<string, string> = {
  fresh:          "#16a34a",
  weakly_stale:   "#a3a3a3",
  strongly_stale: "#d97706",
  contradicted:   "#dc2626",
  tombstoned:     "#a3a3a3",
  unknown:        "#a3a3a3",
};

function nodeFill(n: GraphNode): string {
  return STALENESS_COLOR[n.staleness] ?? "#a3a3a3";
}

function nodeStroke(n: GraphNode): string {
  if (n.critical) return "#dc2626";
  if (n.leased)   return "#2563eb";
  return "#e5e5e5";
}

function nodeStrokeWidth(n: GraphNode): number {
  if (n.critical) return 2;
  if (n.leased)   return 2;
  return 1;
}

function nodeRadius(n: GraphNode): number {
  if (n.ghost) return 3;
  return 4 + Math.min(10, Math.log2(1 + n.rev_deps) * 1.6);
}

function edgeColor(e: GraphEdge): string {
  return e.kind === "replaced_by" ? "#a3a3a3" : "#cbd5e1";
}

function edgeDash(e: GraphEdge): string {
  return e.kind === "replaced_by" ? "4 4" : "";
}

// ── Simulation typing ──────────────────────────────────────────────────────

type SimNode  = GraphNode & SimulationNodeDatum;
type SimLink  = SimulationLinkDatum<SimNode> & { kind: string };

// ── Component ─────────────────────────────────────────────────────────────

export function GraphView() {
  const svgRef    = useRef<SVGSVGElement | null>(null);
  const innerGRef = useRef<SVGGElement | null>(null);

  const showGhosts          = useStore((s) => s.showGhosts);
  const setShowGhosts       = useStore((s) => s.setShowGhosts);
  const setSelectedLifeline = useStore((s) => s.setSelectedLifeline);

  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);

  // Fetch graph data + ghost-toggle reactively.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.graph(showGhosts)
      .then((g) => {
        if (cancelled) return;
        setNodes(g.nodes);
        setEdges(g.edges);
        setWarning(
          g.node_count > 5000
            ? `large graph (${g.node_count} nodes) — interaction may be slow`
            : null
        );
      })
      .catch((e) => !cancelled && setWarning(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [showGhosts]);

  // Run the force simulation + render every tick. We build SimNode + SimLink
  // arrays from the props; d3-force mutates them in place with x/y. The
  // result is read back inside the tick handler to update SVG transforms.
  useEffect(() => {
    if (!svgRef.current || !innerGRef.current) return;
    if (nodes.length === 0) {
      simRef.current?.stop();
      simRef.current = null;
      return;
    }

    const simNodes: SimNode[] = nodes.map((n) => ({ ...n }));
    const idIndex = new Map(simNodes.map((n, i) => [n.id, i]));
    const simLinks: SimLink[] = edges
      .filter((e) => idIndex.has(e.source) && idIndex.has(e.target))
      .map((e) => ({ source: e.source, target: e.target, kind: e.kind }));

    const sim = forceSimulation<SimNode, SimLink>(simNodes)
      .force("link", forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id).distance(28).strength(0.6))
      .force("charge", forceManyBody().strength(-80))
      .force("collide", forceCollide<SimNode>().radius((d) => nodeRadius(d) + 2))
      .force("center", forceCenter(0, 0))
      .alpha(1)
      .alphaDecay(0.04);
    simRef.current = sim;

    const inner = select(innerGRef.current);
    inner.selectAll("*").remove();

    const linkG = inner.append("g").attr("class", "links")
      .attr("fill", "none").attr("stroke-opacity", 0.6);
    const nodeG = inner.append("g").attr("class", "nodes");

    const linkSel = linkG.selectAll("line").data(simLinks).join("line")
      .attr("stroke", (d: any) => edgeColor(d))
      .attr("stroke-width", 1.2)
      .attr("stroke-dasharray", (d: any) => edgeDash(d));

    const nodeSel = nodeG.selectAll<SVGCircleElement, SimNode>("circle")
      .data(simNodes, (d) => d.id)
      .join("circle")
      .attr("r", (d) => nodeRadius(d))
      .attr("fill", (d) => nodeFill(d))
      .attr("fill-opacity", (d) => d.ghost ? 0.35 : 1)
      .attr("stroke", (d) => nodeStroke(d))
      .attr("stroke-width", (d) => nodeStrokeWidth(d))
      .style("cursor", "pointer")
      .on("click", (_event, d) => setSelectedLifeline(d.id))
      .call(
        drag<SVGCircleElement, SimNode>()
          .on("start", (event, d) => {
            if (!event.active) sim.alphaTarget(0.3).restart();
            d.fx = d.x; d.fy = d.y;
          })
          .on("drag",  (event, d) => { d.fx = event.x; d.fy = event.y; })
          .on("end",   (event, d) => {
            if (!event.active) sim.alphaTarget(0);
            d.fx = null; d.fy = null;
          })
      );

    nodeSel.append("title").text((d) => `${d.path ?? "(tombstoned)"}\n${d.staleness}`);

    sim.on("tick", () => {
      linkSel
        .attr("x1", (d: any) => (d.source as SimNode).x ?? 0)
        .attr("y1", (d: any) => (d.source as SimNode).y ?? 0)
        .attr("x2", (d: any) => (d.target as SimNode).x ?? 0)
        .attr("y2", (d: any) => (d.target as SimNode).y ?? 0);
      nodeSel
        .attr("cx", (d) => d.x ?? 0)
        .attr("cy", (d) => d.y ?? 0);
    });

    // Pan + zoom — the inner <g> gets the transform.
    const zoomBehavior: ZoomBehavior<SVGSVGElement, unknown> = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.2, 8])
      .on("zoom", (event) => {
        inner.attr("transform", event.transform.toString());
      });
    select(svgRef.current).call(zoomBehavior);
    select(svgRef.current).call(zoomBehavior.transform, zoomIdentity);

    return () => {
      sim.stop();
    };
  }, [nodes, edges, setSelectedLifeline]);

  return (
    <div className="relative h-full w-full bg-white overflow-hidden">
      <svg ref={svgRef} className="absolute inset-0 w-full h-full"
           viewBox="-300 -300 600 600"
           preserveAspectRatio="xMidYMid meet">
        <g ref={innerGRef} />
      </svg>
      <div className="absolute top-2 right-2 flex items-center gap-2 rounded bg-white/90 border border-line px-2 py-1 text-xs shadow-sm">
        <label className="flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={showGhosts}
            onChange={(e) => setShowGhosts(e.target.checked)}
          />
          <span>show ghost nodes</span>
        </label>
        <span className="text-muted">·</span>
        <span className="font-mono text-muted">
          {nodes.length}n / {edges.length}e
        </span>
      </div>
      {loading && (
        <div className="absolute bottom-2 left-2 text-xs text-muted bg-white/80 px-2 py-1 rounded">
          loading…
        </div>
      )}
      {warning && (
        <div className="absolute bottom-2 right-2 text-xs text-warn bg-amber-50 border border-warn/40 px-2 py-1 rounded max-w-md">
          {warning}
        </div>
      )}
      {nodes.length === 0 && !loading && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-muted pointer-events-none">
          no lifelines yet — `projmem index` to populate
        </div>
      )}
    </div>
  );
}
