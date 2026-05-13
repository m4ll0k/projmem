import { useEffect, useRef, useState } from "react";
import {
  forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide,
  forceX, forceY,
  Simulation, SimulationNodeDatum, SimulationLinkDatum,
} from "d3-force";
import { select } from "d3-selection";
import { zoom, ZoomBehavior, zoomIdentity, zoomTransform } from "d3-zoom";
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
  if (n.symbol) return "#fef3c7";
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
  if (n.symbol) return 4;
  if (n.ghost)  return 3;
  return 5 + Math.min(14, Math.log2(1 + n.rev_deps) * 2);
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
  return e.kind === "contains" ? 0.6 : 1.1;
}

function truncLabel(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + "…";
}

// Stable hash to drive directory-clustered force anchors. We want every
// directory to consistently land in the same region of the canvas so
// re-renders + ghost toggles don't shuffle the whole graph.
function dirOf(path: string | null): string {
  if (!path) return "";
  const i = path.lastIndexOf("/");
  return i >= 0 ? path.slice(0, i) : "";
}
function hash32(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}
function clusterCoords(dir: string, n: number): { x: number; y: number } {
  // Place each unique directory at a slot around a big circle.
  // The slot is a stable function of the directory string.
  const slot = hash32(dir) % Math.max(8, n);
  const angle = (slot / Math.max(8, n)) * Math.PI * 2;
  const radius = 280;
  return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
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
  const liveLeasedPaths     = useStore((s) => s.liveLeasedPaths);

  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [pinnedCount, setPinnedCount] = useState(0);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const simRef     = useRef<Simulation<SimNode, SimLink> | null>(null);
  const simNodesRef = useRef<SimNode[]>([]);
  const zoomBehaviorRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);

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
    simNodesRef.current = simNodes;
    const idIndex = new Map(simNodes.map((n) => [n.id, n]));
    const simLinks: SimLink[] = edges
      .filter((e) => idIndex.has(e.source) && idIndex.has(e.target))
      .map((e) => ({ source: e.source, target: e.target, kind: e.kind }));

    // Count unique directories for cluster anchor distribution.
    const dirSet = new Set(simNodes.map((n) => dirOf(n.path)));
    const dirCount = dirSet.size;
    // Seed each node's initial position near its cluster anchor — the
    // first tick already starts spread out instead of all-at-origin.
    for (const n of simNodes) {
      const dir = dirOf(n.path);
      const c = clusterCoords(dir, dirCount);
      if (n.x === undefined) n.x = c.x + (Math.random() - 0.5) * 40;
      if (n.y === undefined) n.y = c.y + (Math.random() - 0.5) * 40;
    }

    // Stronger forces than the v2 first-pass — the previous tuning
    // produced a hairball on real repos (250+ files / 800+ edges).
    const sim = forceSimulation<SimNode, SimLink>(simNodes)
      .force("link", forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id)
        .distance((l) => (l.kind === "contains" ? 18 : 55))
        .strength((l) => (l.kind === "contains" ? 0.95 : 0.4)))
      .force("charge", forceManyBody().strength((d: any) =>
        (d as GraphNode).symbol ? -50 : -260).distanceMax(450))
      .force("collide", forceCollide<SimNode>()
        .radius((d) => nodeRadius(d) + 4).strength(1))
      // Directory clustering: pull each node toward its directory's
      // anchor point. Weak strength so links still dominate, strong
      // enough that unrelated directories don't intermix.
      .force("xCluster", forceX<SimNode>(
        (d) => clusterCoords(dirOf(d.path), dirCount).x).strength(0.06))
      .force("yCluster", forceY<SimNode>(
        (d) => clusterCoords(dirOf(d.path), dirCount).y).strength(0.06))
      .force("center", forceCenter(0, 0).strength(0.02))
      .alpha(1)
      .alphaDecay(0.03);
    simRef.current = sim;

    const inner = select(innerGRef.current);
    inner.selectAll("*").remove();

    const linkG = inner.append("g").attr("class", "links")
      .attr("fill", "none").attr("stroke-opacity", 0.45);
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
      .on("mouseenter", (_e, d) => setHoveredId(d.id))
      .on("mouseleave", () => setHoveredId(null))
      .on("click", (_event, d) => {
        if (d.symbol && d.path) {
          const parent = simNodes.find((m) => !m.symbol && m.path === d.path);
          if (parent) setSelectedLifeline(parent.id);
        } else {
          setSelectedLifeline(d.id);
        }
      })
      .on("dblclick", (event, d) => {
        // Double-click toggles a node's pin state.
        event.stopPropagation();
        if (d.fx != null || d.fy != null) {
          d.fx = null; d.fy = null;
        } else {
          d.fx = d.x ?? 0; d.fy = d.y ?? 0;
        }
        setPinnedCount(simNodes.filter((n) => n.fx != null).length);
        sim.alpha(0.3).restart();
      })
      .call(
        drag<SVGGElement, SimNode>()
          .on("start", (event, d) => {
            if (!event.active) sim.alphaTarget(0.3).restart();
            d.fx = d.x; d.fy = d.y;
          })
          .on("drag", (event, d) => { d.fx = event.x; d.fy = event.y; })
          .on("end", (event, d) => {
            if (!event.active) sim.alphaTarget(0);
            // KEEP fx/fy set — drag is "pin here forever" until
            // dblclick or "release pins" releases it. This is what
            // the user asked for: "allow the user to move them".
            setPinnedCount(simNodes.filter((n) => n.fx != null).length);
          })
      );

    // Pulsing halo for live-leased nodes (Claude is editing this file
    // RIGHT NOW). Two overlapping circles whose r + opacity animate
    // 0→1.5× radius outward at 1.2 Hz. The halos are filled blue +
    // transparent so they read as a glow. We always render the halo
    // element but flip `display` based on live-leased state so the
    // simulation tick handler doesn't have to recreate DOM each
    // frame. Animated via SVG SMIL (works in every browser projmem
    // targets; no JS RAF loop needed).
    const halo = node.append("g").attr("class", "halo")
      .attr("pointer-events", "none")
      .style("display", "none");
    halo.append("circle")
      .attr("class", "halo-ring")
      .attr("fill", "#2563eb")
      .attr("fill-opacity", 0.18);
    halo.append("animate")
      .attr("attributeName", "opacity")
      .attr("values", "0.4;0.85;0.4")
      .attr("dur", "1.2s")
      .attr("repeatCount", "indefinite");

    // Circles.
    node.append("circle")
      .attr("r", (d) => nodeRadius(d))
      .attr("fill", (d) => nodeFill(d))
      .attr("fill-opacity", (d) => d.ghost ? 0.35 : 1)
      .attr("stroke", (d) => nodeStroke(d))
      .attr("stroke-width", (d) => nodeStrokeWidth(d));

    // Pin indicator — small dark ring around pinned nodes.
    node.append("circle").attr("class", "pin-ring")
      .attr("r", (d) => nodeRadius(d) + 3)
      .attr("fill", "none")
      .attr("stroke", "#1f2937")
      .attr("stroke-width", 1)
      .attr("stroke-dasharray", "1 2")
      .style("display", (d) => (d.fx != null || d.fy != null) ? "" : "none");

    node.append("title").text((d) => {
      if (d.symbol) return `${d.symbol_kind ?? "symbol"} ${d.label} — ${d.path}:${d.line}`;
      return `${d.path ?? "(tombstoned)"}\n${d.staleness}` +
             "\n\n(drag = pin · double-click = unpin)";
    });

    // Labels. Always render the text element so the showLabel toggle
    // below can flip display without re-creating DOM.
    node.append("text")
      .attr("class", "node-label")
      .attr("x", (d) => nodeRadius(d) + 3)
      .attr("y", 3)
      .attr("font-size", (d) => d.symbol ? 8 : 10)
      .attr("font-family", "ui-monospace, SFMono-Regular, monospace")
      .attr("fill", (d) => d.symbol ? "#92400e" : "#0a0a0a")
      .attr("font-style", (d) => d.symbol ? "italic" : "normal")
      .attr("pointer-events", "none")
      .text((d) => truncLabel(d.label, d.symbol ? 18 : 30));

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
    zoomBehaviorRef.current = zoomBehavior;
    select(svgRef.current).call(zoomBehavior);
    select(svgRef.current).call(zoomBehavior.transform, zoomIdentity);

    return () => { sim.stop(); };
  }, [nodes, edges, setSelectedLifeline]);

  // Label visibility + live-leased halo are both DOM-pass effects.
  // We walk every g.node once and flip flags so the simulation tick
  // handler stays free.
  useEffect(() => {
    const root = innerGRef.current;
    if (!root) return;
    const showAll = zoomLevel >= 1.4;
    const liveSet = new Set(liveLeasedPaths);

    root.querySelectorAll<SVGGElement>("g.node").forEach((g) => {
      const d = (g as any).__data__ as GraphNode | undefined;
      const nid = d?.id;

      // Live-leased halo: visible only while the lease is open.
      const halo = g.querySelector<SVGGElement>("g.halo");
      const haloRing = g.querySelector<SVGCircleElement>("circle.halo-ring");
      const isLive   = !!(d?.path && liveSet.has(d.path));
      if (halo) halo.style.display = isLive ? "" : "none";
      if (haloRing && d) {
        haloRing.setAttribute("r", String(nodeRadius(d) + 12));
      }

      // Dim non-leased nodes when ANY lease is live — focus mode.
      const dim = liveSet.size > 0 && !isLive;
      g.style.opacity = dim ? "0.28" : "1";

      // Labels.
      const label = g.querySelector<SVGTextElement>("text.node-label");
      if (label) {
        const alwaysShow = d?.critical || d?.leased || isLive;
        const isHovered  = hoveredId === nid;
        label.style.display =
          (showAll || alwaysShow || isHovered) ? "" : "none";
      }
    });
  }, [zoomLevel, hoveredId, nodes, liveLeasedPaths]);

  const releasePins = () => {
    for (const n of simNodesRef.current) {
      n.fx = null; n.fy = null;
    }
    setPinnedCount(0);
    simRef.current?.alpha(0.5).restart();
  };

  const fitView = () => {
    if (!svgRef.current || !zoomBehaviorRef.current) return;
    select(svgRef.current).call(
      zoomBehaviorRef.current.transform, zoomIdentity,
    );
  };

  return (
    <div className="relative h-full w-full bg-white overflow-hidden">
      <svg ref={svgRef} className="absolute inset-0 w-full h-full"
           viewBox="-400 -300 800 600"
           preserveAspectRatio="xMidYMid meet">
        <g ref={innerGRef} />
      </svg>

      {/* Top-right control cluster */}
      <div className="absolute top-2 right-2 flex flex-col gap-1 items-end">
        <div className="flex items-center gap-3 rounded bg-white/90 border border-line px-2 py-1 text-xs shadow-sm">
          <label className="flex items-center gap-1 cursor-pointer">
            <input
              type="checkbox"
              checked={showSymbols}
              onChange={(e) => setShowSymbols(e.target.checked)}
            />
            <span>symbols</span>
          </label>
          <label className="flex items-center gap-1 cursor-pointer">
            <input
              type="checkbox"
              checked={showGhosts}
              onChange={(e) => setShowGhosts(e.target.checked)}
            />
            <span>ghosts</span>
          </label>
        </div>
        <div className="flex items-center gap-2 rounded bg-white/90 border border-line px-2 py-1 text-xs shadow-sm">
          <button
            onClick={fitView}
            className="px-1.5 py-0.5 rounded border border-line hover:bg-line/40"
            title="reset zoom"
          >fit</button>
          <button
            onClick={releasePins}
            disabled={pinnedCount === 0}
            className={`px-1.5 py-0.5 rounded border ${
              pinnedCount > 0
                ? "border-warn bg-amber-50 text-warn hover:bg-amber-100"
                : "border-line text-muted"
            }`}
            title="release every pinned node"
          >
            {pinnedCount > 0 ? `release ${pinnedCount} pin${pinnedCount > 1 ? "s" : ""}` : "0 pinned"}
          </button>
        </div>
        <div className="rounded bg-white/90 border border-line px-2 py-0.5 text-[10px] font-mono text-muted shadow-sm">
          {nodes.length}n · {edges.length}e · {zoomLevel.toFixed(1)}×
        </div>
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

      {/* Bottom-left help + legend */}
      <div className="absolute bottom-2 left-2 rounded bg-white/90 border border-line px-2 py-1.5 text-[10px] shadow-sm space-y-0.5 max-w-[240px]">
        <div className="font-semibold tracking-tight mb-1">Legend & controls</div>
        <div className="grid grid-cols-2 gap-x-2">
          <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full" style={{background:"#16a34a"}}/>fresh</div>
          <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full" style={{background:"#d97706"}}/>stale</div>
          <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full" style={{background:"#dc2626"}}/>contradicted</div>
          <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full border-2" style={{background:"#fff", borderColor:"#dc2626"}}/>critical</div>
          <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full border-2" style={{background:"#fff", borderColor:"#2563eb"}}/>leased</div>
          <div className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full" style={{background:"#fef3c7", border:"1px solid #d97706"}}/>symbol</div>
        </div>
        <div className="pt-1 mt-1 border-t border-line text-muted">
          drag · zoom · click — labels at hover or zoom ≥ 1.4×<br />
          drag = pin here · double-click node = unpin
        </div>
      </div>
    </div>
  );
}
