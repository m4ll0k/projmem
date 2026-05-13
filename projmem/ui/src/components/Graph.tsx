import { useEffect, useRef, useState } from "react";
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
  if (n.symbol) return "#fef3c7";                 // soft amber for symbols
  return STALENESS_COLOR[n.staleness] ?? "#a3a3a3";
}

function nodeStroke(n: GraphNode): string {
  if (n.critical)  return "#dc2626";
  if (n.leased)    return "#2563eb";
  if (n.symbol)    return "#d97706";
  return "#e5e5e5";
}

function nodeStrokeWidth(n: GraphNode): number {
  if (n.critical) return 2;
  if (n.leased)   return 2;
  return 1;
}

function nodeRadius(n: GraphNode): number {
  if (n.symbol) return 3.5;
  if (n.ghost)  return 3;
  return 5 + Math.min(12, Math.log2(1 + n.rev_deps) * 1.8);
}

function edgeColor(e: GraphEdge): string {
  if (e.kind === "replaced_by") return "#a3a3a3";
  if (e.kind === "contains")    return "#fbbf24";
  return "#cbd5e1";
}

function edgeDash(e: GraphEdge): string {
  if (e.kind === "replaced_by") return "4 4";
  if (e.kind === "contains")    return "2 3";
  return "";
}

function edgeWidth(e: GraphEdge): number {
  return e.kind === "contains" ? 0.8 : 1.2;
}

function truncLabel(s: string, max = 22): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + "…";
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
  const showSymbols         = useStore((s) => s.showSymbols);
  const setShowSymbols      = useStore((s) => s.setShowSymbols);
  const setSelectedLifeline = useStore((s) => s.setSelectedLifeline);

  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [loading, setLoading] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.graph(showGhosts, showSymbols)
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
  }, [showGhosts, showSymbols]);

  useEffect(() => {
    if (!svgRef.current || !innerGRef.current) return;
    if (nodes.length === 0) {
      simRef.current?.stop();
      simRef.current = null;
      return;
    }

    const simNodes: SimNode[] = nodes.map((n) => ({ ...n }));
    const idIndex = new Map(simNodes.map((n) => [n.id, n]));
    const simLinks: SimLink[] = edges
      .filter((e) => idIndex.has(e.source) && idIndex.has(e.target))
      .map((e) => ({ source: e.source, target: e.target, kind: e.kind }));

    // 'contains' edges keep symbols close to their parent file; other
    // edges use the default distance.
    const sim = forceSimulation<SimNode, SimLink>(simNodes)
      .force("link", forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id)
        .distance((l) => (l.kind === "contains" ? 12 : 32))
        .strength((l) => (l.kind === "contains" ? 0.95 : 0.6)))
      .force("charge", forceManyBody().strength((d: any) =>
        (d as GraphNode).symbol ? -25 : -90))
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
      .attr("stroke-width", (d: any) => edgeWidth(d))
      .attr("stroke-dasharray", (d: any) => edgeDash(d));

    const node = nodeG.selectAll<SVGGElement, SimNode>("g.node")
      .data(simNodes, (d) => d.id)
      .join("g")
      .attr("class", "node")
      .style("cursor", "pointer")
      .on("click", (_event, d) => {
        // Symbol nodes select their parent file's lifeline; file
        // nodes select themselves.
        if (d.symbol && d.path) {
          // Look up parent lifeline by path.
          const parent = simNodes.find(
            (m) => !m.symbol && m.path === d.path,
          );
          if (parent) setSelectedLifeline(parent.id);
        } else {
          setSelectedLifeline(d.id);
        }
      })
      .call(
        drag<SVGGElement, SimNode>()
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

    node.append("circle")
      .attr("r", (d) => nodeRadius(d))
      .attr("fill", (d) => nodeFill(d))
      .attr("fill-opacity", (d) => d.ghost ? 0.35 : 1)
      .attr("stroke", (d) => nodeStroke(d))
      .attr("stroke-width", (d) => nodeStrokeWidth(d));

    node.append("title").text((d) => {
      if (d.symbol) return `${d.symbol_kind ?? "symbol"} ${d.label} — ${d.path}:${d.line}`;
      return `${d.path ?? "(tombstoned)"}\n${d.staleness}`;
    });

    // Labels: alongside the node, not centered (centered text on small
    // circles is unreadable). Symbol nodes get italic labels.
    node.append("text")
      .attr("class", "node-label")
      .attr("x", (d) => nodeRadius(d) + 3)
      .attr("y", 3)
      .attr("font-size", (d) => d.symbol ? 8 : 10)
      .attr("font-family", "ui-monospace, SFMono-Regular, monospace")
      .attr("fill", (d) => d.symbol ? "#92400e" : "#0a0a0a")
      .attr("font-style", (d) => d.symbol ? "italic" : "normal")
      .attr("pointer-events", "none")
      .text((d) => truncLabel(d.label, d.symbol ? 18 : 26));

    sim.on("tick", () => {
      linkSel
        .attr("x1", (d: any) => (d.source as SimNode).x ?? 0)
        .attr("y1", (d: any) => (d.source as SimNode).y ?? 0)
        .attr("x2", (d: any) => (d.target as SimNode).x ?? 0)
        .attr("y2", (d: any) => (d.target as SimNode).y ?? 0);
      node.attr("transform",
        (d) => `translate(${d.x ?? 0}, ${d.y ?? 0})`);
    });

    const zoomBehavior: ZoomBehavior<SVGSVGElement, unknown> =
      zoom<SVGSVGElement, unknown>()
        .scaleExtent([0.1, 12])
        .on("zoom", (event) => {
          inner.attr("transform", event.transform.toString());
          setZoomLevel(event.transform.k);
        });
    select(svgRef.current).call(zoomBehavior);
    select(svgRef.current).call(zoomBehavior.transform, zoomIdentity);

    return () => { sim.stop(); };
  }, [nodes, edges, setSelectedLifeline]);

  // Hide labels at very low zoom so the canvas stays readable.
  useEffect(() => {
    if (!innerGRef.current) return;
    const labels = innerGRef.current.querySelectorAll("text.node-label");
    const visible = zoomLevel >= 0.7;
    labels.forEach((el) => {
      (el as SVGTextElement).style.display = visible ? "" : "none";
    });
  }, [zoomLevel, nodes]);

  return (
    <div className="relative h-full w-full bg-white overflow-hidden">
      <svg ref={svgRef} className="absolute inset-0 w-full h-full"
           viewBox="-400 -300 800 600"
           preserveAspectRatio="xMidYMid meet">
        <g ref={innerGRef} />
      </svg>
      <div className="absolute top-2 right-2 flex items-center gap-3 rounded bg-white/90 border border-line px-2 py-1 text-xs shadow-sm">
        <label className="flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={showSymbols}
            onChange={(e) => setShowSymbols(e.target.checked)}
          />
          <span>show symbols</span>
        </label>
        <label className="flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={showGhosts}
            onChange={(e) => setShowGhosts(e.target.checked)}
          />
          <span>show ghosts</span>
        </label>
        <span className="text-muted">·</span>
        <span className="font-mono text-muted">
          {nodes.length}n / {edges.length}e
        </span>
        <span className="text-muted">·</span>
        <span className="font-mono text-muted">{zoomLevel.toFixed(1)}×</span>
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
      <div className="absolute top-2 left-2 rounded bg-white/90 border border-line px-2 py-1 text-[10px] shadow-sm space-y-0.5 max-w-[180px]">
        <div className="font-semibold tracking-tight">Legend</div>
        <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full" style={{background:"#16a34a"}}/> fresh</div>
        <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full" style={{background:"#d97706"}}/> stale</div>
        <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full" style={{background:"#dc2626"}}/> contradicted</div>
        <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full border-2" style={{background:"#fff", borderColor:"#dc2626"}}/> critical</div>
        <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full border-2" style={{background:"#fff", borderColor:"#2563eb"}}/> leased</div>
        <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full italic" style={{background:"#fef3c7", border:"1px solid #d97706"}}/> symbol</div>
      </div>
    </div>
  );
}
