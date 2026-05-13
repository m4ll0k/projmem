import { useEffect, useMemo, useRef, useState } from "react";
import {
  forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide,
  forceX, forceY,
  Simulation, SimulationNodeDatum, SimulationLinkDatum,
} from "d3-force";
import { select } from "d3-selection";
import { zoom, ZoomBehavior, zoomIdentity } from "d3-zoom";
import { drag } from "d3-drag";

import { api } from "../api";
import { useStore } from "../store";
import type { GraphNode, GraphEdge } from "../types";

// ── Theme-aware palette ───────────────────────────────────────────────────
// The SVG renders colors imperatively (d3 sets attributes via JS), so it
// can't use Tailwind classes the way the React components do. We read
// the live CSS variable values on each render and rebuild the palette.
function readPalette(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const s = getComputedStyle(document.documentElement);
  const get = (v: string) => s.getPropertyValue(v).trim();
  return {
    bg:     get("--bg"),
    ink:    get("--ink"),
    muted:  get("--muted"),
    line:   get("--line"),
    accent: get("--accent"),
    good:   get("--good"),
    warn:   get("--warn"),
    bad:    get("--bad"),
    ghost:  get("--ghost"),
    halo:   get("--halo"),
  };
}

// ── Section coloring ─────────────────────────────────────────────────────
// A node's FILL is determined by its TOP-LEVEL directory section
// (the first path segment: `projmem`, `tests`, `bench`, `docs`, …).
// Stable across renders — the section→color mapping is a hash, so the
// same directory always lands on the same hue. This gives the eye a
// fixed map of "where in the codebase am I looking?" that holds even
// when nodes shuffle. State (critical / leased / stale) rides on the
// STROKE so it doesn't fight the section signal.

const SECTION_PALETTE = [
  "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6",
  "#ec4899", "#06b6d4", "#84cc16", "#f97316",
  "#a855f7", "#14b8a6", "#6366f1", "#22c55e",
  "#0ea5e9", "#d946ef", "#65a30d", "#dc2626",
];

function topLevel(path: string | null): string {
  if (!path) return "(root)";
  const i = path.indexOf("/");
  return i >= 0 ? path.slice(0, i) : "(root)";
}

function sectionColor(section: string): string {
  return SECTION_PALETTE[hash32(section) % SECTION_PALETTE.length];
}

function staleStrokeColor(s: string, P: Record<string, string>): string | null {
  switch (s) {
    case "contradicted":   return P.bad;
    case "strongly_stale": return P.warn;
    default:               return null;
  }
}

function nodeFill(n: GraphNode, P: Record<string, string>): string {
  if (n.symbol) return "rgba(251, 191, 36, 0.18)";
  if (n.ghost)  return P.ghost;
  return sectionColor(topLevel(n.path));
}

function nodeStroke(n: GraphNode, P: Record<string, string>): string {
  // Stroke priority: critical (red, hardest signal) > leased (accent) >
  // contradicted/strongly-stale (state). Otherwise a soft outline so
  // the section fill carries the visual weight.
  if (n.critical) return P.bad;
  if (n.leased)   return P.accent;
  if (n.symbol)   return P.warn;
  const stale = staleStrokeColor(n.staleness, P);
  if (stale)      return stale;
  return P.line;
}

function nodeStrokeWidth(n: GraphNode): number {
  if (n.critical) return 2;
  if (n.leased)   return 2;
  if (n.staleness === "contradicted" ||
      n.staleness === "strongly_stale") return 1.5;
  return 1;
}

function nodeRadius(n: GraphNode): number {
  if (n.symbol) return 4;
  if (n.ghost)  return 3;
  return 5 + Math.min(14, Math.log2(1 + n.rev_deps) * 2);
}

function edgeColor(e: GraphEdge, P: Record<string, string>): string {
  if (e.kind === "replaced_by") return P.muted;
  if (e.kind === "contains")    return P.warn;
  return P.line;
}

function nodeShortPath(n: GraphNode): string {
  return (n.path || "").split("/").pop() || "—";
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

// Stable per-directory anchor (gitignore-stable across rerenders).
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
  const slot = hash32(dir) % Math.max(8, n);
  const angle = (slot / Math.max(8, n)) * Math.PI * 2;
  return { x: Math.cos(angle) * 280, y: Math.sin(angle) * 280 };
}

// ── Simulation typing ──────────────────────────────────────────────────────

type SimNode = GraphNode & SimulationNodeDatum;
type SimLink = SimulationLinkDatum<SimNode> & { kind: string };

// ── Component ─────────────────────────────────────────────────────────────

export function GraphView() {
  const svgRef    = useRef<SVGSVGElement | null>(null);
  const innerGRef = useRef<SVGGElement | null>(null);

  const theme               = useStore((s) => s.theme);
  const showGhosts          = useStore((s) => s.showGhosts);
  const setShowGhosts       = useStore((s) => s.setShowGhosts);
  const showSymbols         = useStore((s) => s.showSymbols);
  const setShowSymbols      = useStore((s) => s.setShowSymbols);
  const setSelectedLifeline = useStore((s) => s.setSelectedLifeline);
  const liveLeasedPaths     = useStore((s) => s.liveLeasedPaths);
  const selectedLifeline    = useStore((s) => s.selectedLifeline);

  const [nodes, setNodes]   = useState<GraphNode[]>([]);
  const [edges, setEdges]   = useState<GraphEdge[]>([]);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [pinnedCount, setPinnedCount] = useState(0);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const [refetchTick, setRefetchTick] = useState(0);
  const simRef             = useRef<Simulation<SimNode, SimLink> | null>(null);
  const simNodesRef        = useRef<SimNode[]>([]);
  const zoomBehaviorRef    = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);

  // Read the palette on theme change. `theme` is just the trigger;
  // the actual values come from CSS vars at the moment of render so
  // we never hard-code two duplicate palettes.
  const palette = useMemo<Record<string, string>>(() => readPalette(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [theme]);

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
  }, [showGhosts, showSymbols, refetchTick]);

  // Auto-refetch the graph when structural events arrive (new file
  // lifelines or tombstones). The halo + focus-mode pieces below
  // react to events in real time without a refetch; but
  // node-count-changing events have to refetch /graph because the
  // simulation was built off the original payload. We throttle to
  // 1 refetch per 600ms so a stress-test burst doesn't hammer.
  const events = useStore((s) => s.events);
  const lastFetchedKindCount = useRef(0);
  useEffect(() => {
    const lastFew = events.slice(-10);
    const structural = lastFew.some((e) =>
      e.kind === "created" || e.kind === "deleted" || e.kind === "moved"
    );
    if (!structural) return;
    const t = setTimeout(() => setRefetchTick((n) => n + 1), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.length]);

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

    const dirCount = new Set(simNodes.map((n) => dirOf(n.path))).size;
    for (const n of simNodes) {
      const c = clusterCoords(dirOf(n.path), dirCount);
      if (n.x === undefined) n.x = c.x + (Math.random() - 0.5) * 40;
      if (n.y === undefined) n.y = c.y + (Math.random() - 0.5) * 40;
    }

    const sim = forceSimulation<SimNode, SimLink>(simNodes)
      .force("link", forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id)
        .distance((l) => (l.kind === "contains" ? 18 : 55))
        .strength((l) => (l.kind === "contains" ? 0.95 : 0.4)))
      .force("charge", forceManyBody().strength((d: any) =>
        (d as GraphNode).symbol ? -50 : -260).distanceMax(450))
      .force("collide", forceCollide<SimNode>()
        .radius((d) => nodeRadius(d) + 4).strength(1))
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
      .attr("data-source", (d: any) =>
        typeof d.source === "object" ? d.source.id : d.source)
      .attr("data-target", (d: any) =>
        typeof d.target === "object" ? d.target.id : d.target)
      .attr("stroke", (d: any) => edgeColor(d, palette))
      .attr("stroke-width", (d: any) => edgeWidth(d))
      .attr("stroke-dasharray", (d: any) => edgeDash(d));

    const node = nodeG.selectAll<SVGGElement, SimNode>("g.node")
      .data(simNodes, (d) => d.id)
      .join("g")
      .attr("class", "node")
      .attr("data-id", (d) => d.id)
      .attr("data-path", (d) => d.path ?? "")
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
        event.stopPropagation();
        if (d.fx != null || d.fy != null) { d.fx = null; d.fy = null; }
        else { d.fx = d.x ?? 0; d.fy = d.y ?? 0; }
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
          .on("end", (event) => {
            if (!event.active) sim.alphaTarget(0);
            setPinnedCount(simNodes.filter((n) => n.fx != null).length);
          })
      );

    // Pulsing halo for live-leased nodes — visible only when an event
    // says the path is currently leased. SVG SMIL animates opacity.
    const halo = node.append("g").attr("class", "halo")
      .attr("pointer-events", "none")
      .style("display", "none");
    halo.append("circle")
      .attr("class", "halo-ring")
      .attr("fill", palette.accent)
      .attr("fill-opacity", 0.22);
    halo.append("animate")
      .attr("attributeName", "opacity")
      .attr("values", "0.35;0.9;0.35")
      .attr("dur", "1.2s")
      .attr("repeatCount", "indefinite");

    node.append("circle")
      .attr("r", (d) => nodeRadius(d))
      .attr("fill", (d) => nodeFill(d, palette))
      .attr("fill-opacity", (d) => d.ghost ? 0.35 : 1)
      .attr("stroke", (d) => nodeStroke(d, palette))
      .attr("stroke-width", (d) => nodeStrokeWidth(d));

    node.append("circle").attr("class", "pin-ring")
      .attr("r", (d) => nodeRadius(d) + 3)
      .attr("fill", "none")
      .attr("stroke", palette.ink)
      .attr("stroke-width", 1)
      .attr("stroke-dasharray", "1 2")
      .style("display", (d) => (d.fx != null || d.fy != null) ? "" : "none");

    node.append("title").text((d) => {
      if (d.symbol) return `${d.symbol_kind ?? "symbol"} ${d.label} — ${d.path}:${d.line}`;
      return `${d.path ?? "(tombstoned)"}\n${d.staleness}` +
             "\n\n(drag = pin · double-click = unpin)";
    });

    node.append("text")
      .attr("class", "node-label")
      .attr("x", (d) => nodeRadius(d) + 3)
      .attr("y", 3)
      .attr("font-size", (d) => d.symbol ? 8 : 10)
      .attr("font-family", "ui-monospace, SFMono-Regular, monospace")
      .attr("fill", (d) => d.symbol ? palette.warn : palette.ink)
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
  }, [nodes, edges, setSelectedLifeline, palette]);

  // Focus mode + label visibility + halo. Computed in a DOM-pass effect
  // so the simulation tick handler stays cheap. When ANY lease is live:
  //   * non-leased nodes drop to 0.08 opacity and lose their labels
  //     entirely (the brief: "hide other parts of graph")
  //   * the 1-hop neighborhood of leased nodes stays at 0.55 opacity
  //     so context is still readable
  //   * leased nodes are at 1.0 with halo pulsing
  useEffect(() => {
    const root = innerGRef.current;
    if (!root) return;
    const showAll = zoomLevel >= 1.4;
    const liveSet = new Set(liveLeasedPaths);
    const focusActive = liveSet.size > 0;

    // Selection focus — when a node is selected (via click in graph or
    // tree) and no lease is live, we still dim the rest of the canvas
    // so the click reads as a "show me just this" gesture. Live-lease
    // focus is stronger (0.04 opacity); selection focus is milder
    // (0.15 opacity) so context stays a glance away.
    const selectionFocus = !focusActive && selectedLifeline != null;
    const anyFocus = focusActive || selectionFocus;

    // Pre-compute 1-hop neighborhood from EITHER lit set (live-leased
    // paths OR the selected lifeline). Both paths feed the same map.
    const neighborIds = new Set<string>();
    const focalIds = new Set<string>();
    if (anyFocus) {
      root.querySelectorAll<SVGGElement>("g.node").forEach((g) => {
        const p = g.getAttribute("data-path");
        const id = g.getAttribute("data-id") || "";
        if (p && liveSet.has(p)) focalIds.add(id);
        if (selectionFocus && id === selectedLifeline) focalIds.add(id);
      });
      root.querySelectorAll<SVGLineElement>("g.links line").forEach((l) => {
        const s = l.getAttribute("data-source") || "";
        const t = l.getAttribute("data-target") || "";
        if (focalIds.has(s)) neighborIds.add(t);
        if (focalIds.has(t)) neighborIds.add(s);
      });
    }

    // Spotlight: when focus mode is active, also keep every file in
    // the SAME DIRECTORY SUBTREE as a leased file lit (at 0.4). Adds
    // the "lit branch" effect on top of the 1-hop import neighborhood.
    const litDirPrefixes = new Set<string>();
    if (focusActive) {
      for (const p of liveSet) {
        const i = p.lastIndexOf("/");
        if (i >= 0) litDirPrefixes.add(p.slice(0, i + 1));
        // Also any directory above this one — the lit branch goes all
        // the way down to the root of the codebase.
        let parent = p.slice(0, i);
        while (parent) {
          litDirPrefixes.add(parent + "/");
          const j = parent.lastIndexOf("/");
          parent = j >= 0 ? parent.slice(0, j) : "";
        }
      }
    }
    const inLitBranch = (path: string | null): boolean => {
      if (!path) return false;
      for (const pre of litDirPrefixes) {
        if (path.startsWith(pre)) return true;
      }
      return false;
    };

    root.querySelectorAll<SVGGElement>("g.node").forEach((g) => {
      const d = (g as any).__data__ as GraphNode | undefined;
      const nid = d?.id ?? "";
      const isLive    = !!(d?.path && liveSet.has(d.path));
      const isSelected = selectionFocus && nid === selectedLifeline;
      const isFocal    = isLive || isSelected;
      const isNeighbor = !isFocal && neighborIds.has(nid);
      const inBranch   = !isFocal && !isNeighbor && inLitBranch(d?.path ?? null);

      // Halo for live-leased; selected gets a static ring (drawn via
      // the existing pin-ring element with a brighter stroke).
      const halo = g.querySelector<SVGGElement>("g.halo");
      const haloRing = g.querySelector<SVGCircleElement>("circle.halo-ring");
      if (halo) halo.style.display = isLive ? "" : "none";
      if (haloRing && d) {
        haloRing.setAttribute("r", String(nodeRadius(d) + 12));
      }
      const pinRing = g.querySelector<SVGCircleElement>("circle.pin-ring");
      if (pinRing && d) {
        // Reuse the pin ring as a selection ring when the node is
        // selected (focal but not live). Pin-state takes priority.
        const dn = d as SimNode;
        const isPinned = dn.fx != null || dn.fy != null;
        if (isPinned) {
          pinRing.style.display = "";
          pinRing.setAttribute("stroke", palette.ink);
          pinRing.setAttribute("stroke-dasharray", "1 2");
          pinRing.setAttribute("r", String(nodeRadius(d) + 3));
        } else if (isSelected) {
          pinRing.style.display = "";
          pinRing.setAttribute("stroke", palette.accent);
          pinRing.setAttribute("stroke-dasharray", "0");
          pinRing.setAttribute("r", String(nodeRadius(d) + 5));
        } else {
          pinRing.style.display = "none";
        }
      }

      // Opacity — live-lease spotlight is stronger than selection.
      let opacity = "1";
      if (focusActive) {
        if (isFocal)         opacity = "1";
        else if (isNeighbor) opacity = "0.55";
        else if (inBranch)   opacity = "0.40";
        else                 opacity = "0.04";
      } else if (selectionFocus) {
        if (isFocal)         opacity = "1";
        else if (isNeighbor) opacity = "0.65";
        else                 opacity = "0.15";
      }
      g.style.opacity = opacity;

      // Labels.
      const label = g.querySelector<SVGTextElement>("text.node-label");
      if (label) {
        const alwaysShow = d?.critical || isFocal;
        const isHovered  = hoveredId === nid;
        let show = showAll || alwaysShow || isHovered;
        if (anyFocus && !isFocal && !isNeighbor && !isHovered) show = false;
        label.style.display = show ? "" : "none";
      }
    });

    // Edges connected to focal (live or selected) nodes brighten;
    // everything else dims. The opacity floor is gentler for
    // selection focus than live-lease focus.
    root.querySelectorAll<SVGLineElement>("g.links line").forEach((l) => {
      const s = l.getAttribute("data-source") || "";
      const t = l.getAttribute("data-target") || "";
      const involves = anyFocus && (
        focalIds.has(s) || focalIds.has(t) ||
        neighborIds.has(s) || neighborIds.has(t)
      );
      if (focusActive) {
        l.style.opacity = involves ? "0.85" : "0.08";
      } else if (selectionFocus) {
        l.style.opacity = involves ? "0.85" : "0.18";
      } else {
        l.style.opacity = "0.45";
      }
    });
  }, [zoomLevel, hoveredId, nodes, liveLeasedPaths, selectedLifeline]);

  const releasePins = () => {
    for (const n of simNodesRef.current) { n.fx = null; n.fy = null; }
    setPinnedCount(0);
    simRef.current?.alpha(0.5).restart();
  };

  const fitView = () => {
    if (!svgRef.current || !zoomBehaviorRef.current) return;
    select(svgRef.current).call(
      zoomBehaviorRef.current.transform, zoomIdentity,
    );
  };

  // When the operator selects a node from the tree (or any other
  // source), pan + zoom the graph to center it. Read selectedLifeline
  // from the store and animate the existing zoom transform.
  useEffect(() => {
    if (!selectedLifeline) return;
    if (!svgRef.current || !zoomBehaviorRef.current) return;
    // Wait a frame for the sim to settle if it's still moving things.
    const t = setTimeout(() => {
      const node = simNodesRef.current.find((n) => n.id === selectedLifeline);
      if (!node || node.x == null || node.y == null) return;
      const targetT = zoomIdentity.scale(2).translate(-node.x, -node.y);
      // No transition — d3-transition adds 30+ KB and a hard pan reads
      // as "snap to focus" which is what the user expects.
      select(svgRef.current!).call(
        zoomBehaviorRef.current!.transform, targetT,
      );
    }, 100);
    return () => clearTimeout(t);
  }, [selectedLifeline]);

  return (
    <div className="relative h-full w-full bg-bg overflow-hidden">
      <svg ref={svgRef} className="absolute inset-0 w-full h-full"
           viewBox="-400 -300 800 600"
           preserveAspectRatio="xMidYMid meet">
        <g ref={innerGRef} />
      </svg>

      <div className="absolute top-2 right-2 flex flex-col gap-1 items-end">
        <div className="flex items-center gap-3 rounded-md bg-elev/95 border border-line px-2 py-1 text-xs shadow-soft text-ink">
          <label className="flex items-center gap-1 cursor-pointer">
            <input type="checkbox" checked={showSymbols}
                   onChange={(e) => setShowSymbols(e.target.checked)} />
            <span>symbols</span>
          </label>
          <label className="flex items-center gap-1 cursor-pointer"
                 title="Tombstoned lifelines — files that were deleted via `projmem deleting`. The lifeline + all its history survives forever; with this toggle on, they appear as faded ghost nodes connected to their replacement files via dashed edges. Use this to spot recreations of files you deliberately killed.">
            <input type="checkbox" checked={showGhosts}
                   onChange={(e) => setShowGhosts(e.target.checked)} />
            <span>ghosts ⓘ</span>
          </label>
        </div>
        <div className="flex items-center gap-2 rounded-md bg-elev/95 border border-line px-2 py-1 text-xs shadow-soft">
          <button onClick={fitView}
                  className="px-1.5 py-0.5 rounded border border-line text-ink hover:bg-sunken">
            fit
          </button>
          <button onClick={releasePins}
                  disabled={pinnedCount === 0}
                  className={`px-1.5 py-0.5 rounded border ${
                    pinnedCount > 0
                      ? "border-warn text-warn bg-warn/10 hover:bg-warn/20"
                      : "border-line text-muted"
                  }`}
                  title="release every pinned node">
            {pinnedCount > 0 ? `release ${pinnedCount}` : "0 pinned"}
          </button>
        </div>
        <div className="rounded-md bg-elev/95 border border-line px-2 py-0.5 text-[10px] font-mono text-muted shadow-soft tabular-nums">
          {nodes.length}n · {edges.length}e · {zoomLevel.toFixed(1)}×
        </div>
      </div>

      {loading && (
        <div className="absolute bottom-2 left-2 text-xs text-muted bg-elev/95 px-2 py-1 rounded">
          loading…
        </div>
      )}
      {warning && (
        <div className="absolute bottom-2 right-2 text-xs text-warn bg-warn/10 border border-warn/30 px-2 py-1 rounded max-w-md">
          {warning}
        </div>
      )}
      {nodes.length === 0 && !loading && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-muted pointer-events-none">
          no lifelines yet — `projmem index` to populate
        </div>
      )}

      <div className="absolute bottom-2 left-2 rounded-md bg-elev/95 border border-line px-2 py-1.5 text-[10px] shadow-soft max-w-[280px] text-ink">
        <div className="font-semibold tracking-tight mb-1">Sections</div>
        {/* Section legend — pulls top-level dirs from the current nodes,
            picks the same colors the graph uses. Sorted by node count
            so the biggest sections lead. */}
        {(() => {
          const counts: Record<string, number> = {};
          for (const n of nodes) {
            if (n.symbol || n.ghost) continue;
            const s = topLevel(n.path);
            counts[s] = (counts[s] || 0) + 1;
          }
          const sorted = Object.entries(counts)
            .sort((a, b) => b[1] - a[1]).slice(0, 8);
          if (sorted.length === 0) {
            return <div className="text-muted">(no sections yet)</div>;
          }
          return (
            <div className="grid grid-cols-2 gap-x-2 gap-y-0.5">
              {sorted.map(([s, n]) => (
                <div key={s} className="flex items-center gap-1">
                  <span className="inline-block w-3 h-3 rounded-full flex-shrink-0"
                        style={{ background: sectionColor(s) }} />
                  <span className="font-mono truncate" title={s}>{s}</span>
                  <span className="text-muted tabular-nums">{n}</span>
                </div>
              ))}
            </div>
          );
        })()}
        <div className="pt-1 mt-1 border-t border-line">
          <div className="font-semibold tracking-tight mb-0.5">State</div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full bg-bg border-2 border-accent"/>leased</span>
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full bg-bg border-2 border-bad"/>critical</span>
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded-full bg-bg border-2 border-warn"/>stale</span>
          </div>
        </div>
        <div className="pt-1 mt-1 border-t border-line text-muted">
          drag = pin · dblclick = unpin · zoom + pan supported<br />
          while editing: focus mode dims unrelated nodes to 8%
        </div>
      </div>
    </div>
  );
}
