import { useEffect, useMemo, useRef, useState } from "react";
import { hierarchy, tree, HierarchyPointNode } from "d3-hierarchy";
import { select } from "d3-selection";
import { zoom, ZoomBehavior, zoomIdentity } from "d3-zoom";
import { linkVertical } from "d3-shape";

import { api } from "../api";
import { useStore } from "../store";
import type { GraphNode } from "../types";

// ── Section palette — same hash function the force-directed graph uses,
//    so directories keep the same color across both views.
const SECTION_PALETTE = [
  "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6",
  "#ec4899", "#06b6d4", "#84cc16", "#f97316",
  "#a855f7", "#14b8a6", "#6366f1", "#22c55e",
  "#0ea5e9", "#d946ef", "#65a30d", "#dc2626",
];
function hash32(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}
function topLevel(path: string | null): string {
  if (!path) return "(root)";
  const i = path.indexOf("/");
  return i >= 0 ? path.slice(0, i) : "(root)";
}
function sectionColor(s: string): string {
  return SECTION_PALETTE[hash32(s) % SECTION_PALETTE.length];
}

// ── Hierarchy datum ───────────────────────────────────────────────────────

interface TreeDatum {
  name:     string;
  path:     string;
  kind:     "root" | "dir" | "file";
  node?:    GraphNode;       // present for files (the original lifeline)
  children?: TreeDatum[];
}

function buildHierarchy(nodes: GraphNode[]): TreeDatum {
  const root: TreeDatum = { name: "/", path: "", kind: "root", children: [] };
  const dirIndex = new Map<string, TreeDatum>();
  dirIndex.set("", root);

  for (const n of nodes) {
    if (n.symbol || n.ghost) continue;
    const p = n.path ?? "";
    const parts = p.split("/").filter(Boolean);
    if (parts.length === 0) continue;
    // Walk / create directory chain.
    let parent = root;
    let accum = "";
    for (let i = 0; i < parts.length - 1; i++) {
      accum = accum ? accum + "/" + parts[i] : parts[i];
      let child = dirIndex.get(accum);
      if (!child) {
        child = {
          name: parts[i], path: accum, kind: "dir", children: [],
        };
        dirIndex.set(accum, child);
        parent.children!.push(child);
      }
      parent = child;
    }
    // File leaf.
    parent.children!.push({
      name: parts[parts.length - 1], path: p, kind: "file", node: n,
    });
  }
  // Sort children of every dir for stable layout.
  const sort = (d: TreeDatum) => {
    if (!d.children) return;
    d.children.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    d.children.forEach(sort);
  };
  sort(root);
  return root;
}

// ── Component ─────────────────────────────────────────────────────────────

export function GraphSchema() {
  const svgRef    = useRef<SVGSVGElement | null>(null);
  const innerGRef = useRef<SVGGElement | null>(null);

  const showGhosts          = useStore((s) => s.showGhosts);
  const setShowGhosts       = useStore((s) => s.setShowGhosts);
  const setSelectedLifeline = useStore((s) => s.setSelectedLifeline);
  const selectedLifeline    = useStore((s) => s.selectedLifeline);
  const liveLeasedPaths     = useStore((s) => s.liveLeasedPaths);
  const theme               = useStore((s) => s.theme);
  const events              = useStore((s) => s.events);

  const [rawNodes, setRawNodes] = useState<GraphNode[]>([]);
  const [refetchTick, setRefetchTick] = useState(0);
  const [zoomLevel, setZoomLevel]     = useState(1);
  const zoomBehaviorRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);

  useEffect(() => {
    const lastFew = events.slice(-10);
    if (!lastFew.some((e) =>
      e.kind === "created" || e.kind === "deleted" || e.kind === "moved")) {
      return;
    }
    const t = setTimeout(() => setRefetchTick((n) => n + 1), 400);
    return () => clearTimeout(t);
  }, [events.length]);

  useEffect(() => {
    let cancelled = false;
    api.graph(showGhosts, false)
      .then((g) => !cancelled && setRawNodes(g.nodes))
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, [showGhosts, refetchTick]);

  const root = useMemo(() => buildHierarchy(rawNodes), [rawNodes]);

  // Layout. We tune `size` based on node count so big repos get room
  // to breathe without stretching small ones into noodles.
  useEffect(() => {
    if (!svgRef.current || !innerGRef.current) return;
    const totalNodes = (function count(d: TreeDatum): number {
      return 1 + (d.children?.reduce((s, c) => s + count(c), 0) ?? 0);
    })(root);
    const maxDepth = (function depth(d: TreeDatum, dep = 0): number {
      if (!d.children?.length) return dep;
      return Math.max(...d.children.map((c) => depth(c, dep + 1)));
    })(root);

    const width  = Math.max(800, totalNodes * 14);
    const height = Math.max(400, maxDepth * 80);

    const h = hierarchy<TreeDatum>(root);
    const layout = tree<TreeDatum>().size([width, height])
      .separation((a, b) => a.parent === b.parent ? 1 : 1.3);
    const positioned = layout(h);

    const inner = select(innerGRef.current);
    inner.selectAll("*").remove();

    const link = linkVertical<any, HierarchyPointNode<TreeDatum>>()
      .x((d) => d.x)
      .y((d) => d.y);

    // Edges first (so nodes paint on top).
    inner.append("g").attr("class", "edges")
      .attr("fill", "none")
      .attr("stroke-opacity", 0.4)
      .selectAll("path")
      .data(positioned.links())
      .join("path")
      .attr("class", (d: any) =>
        "edge edge-from-" + (d.source.data.path || "root").replace(/[^\w]/g, "_"))
      .attr("data-source-path", (d: any) => d.source.data.path || "")
      .attr("data-target-path", (d: any) => d.target.data.path || "")
      .attr("stroke", (d: any) => {
        const dirName = topLevel(d.target.data.path || d.source.data.path);
        return sectionColor(dirName);
      })
      .attr("stroke-width", 1)
      .attr("d", (d: any) => link({ source: d.source, target: d.target }) || "");

    // Node groups.
    const nodeG = inner.append("g").attr("class", "nodes");
    const nodeSel = nodeG.selectAll<SVGGElement, HierarchyPointNode<TreeDatum>>("g.node")
      .data(positioned.descendants())
      .join("g")
      .attr("class", "node")
      .attr("data-path", (d) => d.data.path || "")
      .attr("transform", (d) => `translate(${d.x}, ${d.y})`)
      .style("cursor", "pointer")
      .on("click", (_e, d) => {
        const id = d.data.node?.id;
        if (id) setSelectedLifeline(id);
      });

    nodeSel.append("circle")
      .attr("r", (d) => d.data.kind === "root" ? 5
                       : d.data.kind === "dir" ? 4 : 3.5)
      .attr("fill", (d) => {
        if (d.data.kind === "root") return "#71717a";
        return sectionColor(topLevel(d.data.path));
      })
      .attr("fill-opacity", (d) => d.data.kind === "file" ? 1 : 0.55)
      .attr("stroke", (d) => {
        const n = d.data.node;
        if (n?.critical) return "#dc2626";
        if (n?.leased)   return "#2563eb";
        return "#e5e5e5";
      })
      .attr("stroke-width", (d) => {
        const n = d.data.node;
        return (n?.critical || n?.leased) ? 2 : 1;
      });

    nodeSel.append("text")
      .attr("dy", (d) => d.children ? -8 : 4)
      .attr("x", (d) => d.children ? 0 : 8)
      .attr("text-anchor", (d) => d.children ? "middle" : "start")
      .attr("font-size", (d) => d.data.kind === "dir" ? 11 : 10)
      .attr("font-family", "ui-monospace, SFMono-Regular, monospace")
      .attr("fill", "currentColor")
      .attr("pointer-events", "none")
      .text((d) => d.data.name);

    nodeSel.append("title").text((d) => d.data.path || "/");

    // Zoom + pan.
    const z: ZoomBehavior<SVGSVGElement, unknown> =
      zoom<SVGSVGElement, unknown>().scaleExtent([0.2, 8])
        .on("zoom", (ev) => {
          inner.attr("transform", ev.transform.toString());
          setZoomLevel(ev.transform.k);
        });
    zoomBehaviorRef.current = z;
    const svg = select(svgRef.current);
    svg.call(z);
    // Center on initial render: translate so the root is at the top
    // of the visible area.
    svg.call(z.transform, zoomIdentity.translate(width / 2, 40));
  }, [root, theme, setSelectedLifeline]);

  // Live-leased highlight: pulse the leaf node + bold its
  // root-to-leaf path. Implemented as a DOM-pass effect so the layout
  // doesn't rebuild when leases come and go.
  useEffect(() => {
    const root_ = innerGRef.current;
    if (!root_) return;
    const liveSet = new Set(liveLeasedPaths);
    const focusActive = liveSet.size > 0;

    // Build the set of ancestor directory paths for every live path.
    const litPathSet = new Set<string>();
    if (focusActive) {
      for (const p of liveSet) {
        const parts = p.split("/").filter(Boolean);
        let accum = "";
        litPathSet.add("");           // root
        for (let i = 0; i < parts.length; i++) {
          accum = accum ? accum + "/" + parts[i] : parts[i];
          litPathSet.add(accum);
        }
      }
    }

    root_.querySelectorAll<SVGGElement>("g.node").forEach((g) => {
      const p = g.getAttribute("data-path") || "";
      const isLive = liveSet.has(p);
      const isLit  = litPathSet.has(p);
      if (focusActive) {
        g.style.opacity = isLive ? "1" : (isLit ? "0.95" : "0.06");
      } else {
        g.style.opacity = "1";
      }
      const circle = g.querySelector<SVGCircleElement>("circle");
      if (circle && isLive) {
        circle.setAttribute("r", "6");
        circle.setAttribute("stroke", "#2563eb");
        circle.setAttribute("stroke-width", "2.5");
      } else if (circle) {
        // Restore default radius based on kind.
        const tag = g.querySelector<SVGTextElement>("text")?.textContent ?? "";
        circle.setAttribute("r", p === ""
          ? "5"
          : (g.querySelector("text")?.getAttribute("x") === "0" ? "4" : "3.5"));
      }
    });

    root_.querySelectorAll<SVGPathElement>("g.edges path").forEach((l) => {
      const s = l.getAttribute("data-source-path") || "";
      const t = l.getAttribute("data-target-path") || "";
      const onLit = focusActive && (litPathSet.has(s) && litPathSet.has(t));
      l.style.opacity = focusActive ? (onLit ? "0.9" : "0.05") : "0.4";
      l.setAttribute("stroke-width", onLit ? "2" : "1");
    });

    // Highlight the selected node ring even when no live lease.
    if (selectedLifeline) {
      root_.querySelectorAll<SVGGElement>("g.node").forEach((g) => {
        const p = g.getAttribute("data-path") || "";
        // We can't look up the lifeline_id directly here, so we
        // approximate: the file whose node==selectedLifeline.
        const datum = (g as any).__data__?.data?.node;
        if (datum?.id === selectedLifeline) {
          const c = g.querySelector<SVGCircleElement>("circle");
          if (c) {
            c.setAttribute("stroke", "#2563eb");
            c.setAttribute("stroke-width", "3");
          }
        }
      });
    }
  }, [liveLeasedPaths, selectedLifeline, root]);

  return (
    <div className="relative h-full w-full bg-bg overflow-hidden">
      <svg ref={svgRef} className="absolute inset-0 w-full h-full text-ink"
           preserveAspectRatio="xMidYMid meet">
        <g ref={innerGRef} />
      </svg>
      <div className="absolute top-2 right-2 flex items-center gap-2 rounded-md bg-elev/95 border border-line px-2 py-1 text-xs shadow-soft">
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="checkbox" checked={showGhosts}
                 onChange={(e) => setShowGhosts(e.target.checked)} />
          <span>ghosts</span>
        </label>
        <span className="text-muted">·</span>
        <span className="font-mono text-muted tabular-nums">
          {zoomLevel.toFixed(1)}×
        </span>
      </div>
      {rawNodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-muted">
          no lifelines yet — `projmem index` to populate
        </div>
      )}
      <div className="absolute bottom-2 left-2 rounded-md bg-elev/95 border border-line px-2 py-1 text-[10px] text-muted shadow-soft">
        directory hierarchy · scroll to zoom · click a leaf to select<br />
        edges colored by top-level section · live edits light their full path
      </div>
    </div>
  );
}
