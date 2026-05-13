import { useEffect, useMemo, useRef, useState } from "react";
import { hierarchy, tree, HierarchyPointNode } from "d3-hierarchy";
import { select } from "d3-selection";
import { zoom, ZoomBehavior, zoomIdentity } from "d3-zoom";
import { linkHorizontal } from "d3-shape";

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

function nodeRadius(kind: string): number {
  return kind === "root" ? 5 : kind === "dir" ? 4 : 3.5;
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

  const showGhosts            = useStore((s) => s.showGhosts);
  const setShowGhosts         = useStore((s) => s.setShowGhosts);
  const nodeLevel             = useStore((s) => s.nodeLevel);
  const setNodeLevel          = useStore((s) => s.setNodeLevel);
  const setSelectedLifeline   = useStore((s) => s.setSelectedLifeline);
  const setSelectedDirectory  = useStore((s) => s.setSelectedDirectory);
  const selectedLifeline      = useStore((s) => s.selectedLifeline);
  const selectedDirectory     = useStore((s) => s.selectedDirectory);
  const liveLeasedPaths       = useStore((s) => s.liveLeasedPaths);
  const theme                 = useStore((s) => s.theme);
  const events                = useStore((s) => s.events);
  const [hoveredPath, setHoveredPath] = useState<string | null>(null);

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

  const root = useMemo(() => {
    const full = buildHierarchy(rawNodes);
    if (nodeLevel === "all") return full;
    // Filter the hierarchy in-place: dirs-only drops every leaf file
    // (keeping the directory structure visible); files-only keeps only
    // leaves, flattening directories into a single root.
    if (nodeLevel === "dirs") {
      const prune = (d: TreeDatum): TreeDatum => ({
        ...d,
        children: (d.children ?? [])
          .filter((c) => c.kind !== "file")
          .map(prune),
      });
      return prune(full);
    }
    // files-only: collect every file leaf and put them all under root.
    const leaves: TreeDatum[] = [];
    const collect = (d: TreeDatum) => {
      if (d.kind === "file") leaves.push(d);
      for (const c of d.children ?? []) collect(c);
    };
    collect(full);
    return { ...full, children: leaves };
  }, [rawNodes, nodeLevel]);

  // Layout — horizontal arborist (root on the left, depth grows to
  // the right, siblings stack vertically). Gives directory labels
  // unlimited horizontal room instead of fighting same-depth siblings
  // for vertical space. nodeSize() picks a FIXED gap between siblings
  // (16 px) + a FIXED depth spacing (220 px) so a 50-node subtree
  // looks the same density as a 5-node one — bigger repos just grow,
  // they don't compress.
  useEffect(() => {
    if (!svgRef.current || !innerGRef.current) return;

    const h = hierarchy<TreeDatum>(root);
    const layout = tree<TreeDatum>()
      .nodeSize([18, 220])
      .separation((a, b) => a.parent === b.parent ? 1 : 1.4);
    const positioned = layout(h);

    // Horizontal orientation: swap x and y when rendering — d3.tree
    // emits (x, y) where x is the cross-axis and y is depth. For a
    // top-down tree that means x=horizontal, y=vertical. For our
    // sideways tree we want depth=horizontal, siblings=vertical, so
    // we put `node.y` into the SVG x slot and `node.x` into the y
    // slot. The link generator uses linkHorizontal which expects
    // {source: {x, y}, target: {x, y}} pre-swapped.
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    positioned.each((n) => {
      const sx = n.y; const sy = n.x;
      if (sx < minX) minX = sx;
      if (sx > maxX) maxX = sx;
      if (sy < minY) minY = sy;
      if (sy > maxY) maxY = sy;
    });
    const padL = 40;        // leave room for the root label on the left
    const padR = 220;       // leaves get full label room on the right
    const padY = 40;
    const vbX = minX - padL;
    const vbY = minY - padY;
    const vbW = (maxX - minX) + padL + padR;
    const vbH = (maxY - minY) + padY * 2;
    svgRef.current.setAttribute(
      "viewBox", `${vbX} ${vbY} ${vbW} ${vbH}`,
    );
    svgRef.current.setAttribute("preserveAspectRatio", "xMinYMid meet");

    const inner = select(innerGRef.current);
    inner.selectAll("*").remove();

    // Horizontal link generator: expects source/target with (x, y)
    // already in swapped layout coords. We feed it (y, x) pairs so
    // the curve is depth-horizontal.
    const link = linkHorizontal<any, { x: number; y: number }>()
      .x((d) => d.x)
      .y((d) => d.y);

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
      .attr("d", (d: any) => link({
        source: { x: d.source.y, y: d.source.x },
        target: { x: d.target.y, y: d.target.x },
      }) || "");

    // Node groups. Transform uses swapped axes (y → x, x → y) so the
    // tree reads left-to-right.
    const nodeG = inner.append("g").attr("class", "nodes");
    const nodeSel = nodeG.selectAll<SVGGElement, HierarchyPointNode<TreeDatum>>("g.node")
      .data(positioned.descendants())
      .join("g")
      .attr("class", "node")
      .attr("data-path", (d) => d.data.path || "")
      .attr("data-kind", (d) => d.data.kind)
      .attr("transform", (d) => `translate(${d.y}, ${d.x})`)
      .style("cursor", "pointer")
      .on("mouseenter", (_e, d) => setHoveredPath(d.data.path || ""))
      .on("mouseleave", () => setHoveredPath(null))
      .on("click", (_e, d) => {
        // File leaf → lifeline selection. Directory → dir-scoped
        // selection (target is the path + trailing slash, matching
        // the v1 convention `projmem note add 'src/auth/' …`).
        if (d.data.kind === "file" && d.data.node?.id) {
          setSelectedLifeline(d.data.node.id);
        } else if (d.data.kind === "dir") {
          setSelectedDirectory(d.data.path + "/");
        } else if (d.data.kind === "root") {
          setSelectedDirectory("@project");
        }
      });

    nodeSel.append("circle")
      .attr("r", (d) => nodeRadius(d.data.kind))
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

    // Labels are now anchored to the RIGHT of every node. Horizontal
    // layout gives them unlimited room — we no longer need to truncate
    // aggressively to avoid sibling collisions. Full name is shown
    // up to 40 chars, then truncated with the full path still in
    // <title> for hover.
    const truncName = (s: string, max: number = 40): string =>
      s.length <= max ? s : s.slice(0, max - 1) + "…";

    nodeSel.append("text")
      .attr("class", "node-label")
      .attr("dy", 4)
      .attr("x", (d) => nodeRadius(d.data.kind) + 6)
      .attr("text-anchor", "start")
      .attr("font-size", (d) => d.data.kind === "dir" ? 11 : 10)
      .attr("font-weight", (d) => d.data.kind === "dir" ? "600" : "400")
      .attr("font-family", "ui-monospace, SFMono-Regular, monospace")
      .attr("fill", "currentColor")
      .attr("pointer-events", "none")
      .text((d) => {
        if (d.data.kind === "root") return "/";
        const baseLen = d.data.kind === "dir" ? 32 : 40;
        return truncName(d.data.name, baseLen);
      });

    nodeSel.append("title").text((d) => d.data.path || "/");

    // Zoom + pan. The viewBox already fits the content; the user can
    // scroll-zoom in to read labels on dense subtrees.
    const z: ZoomBehavior<SVGSVGElement, unknown> =
      zoom<SVGSVGElement, unknown>().scaleExtent([0.2, 8])
        .on("zoom", (ev) => {
          inner.attr("transform", ev.transform.toString());
          setZoomLevel(ev.transform.k);
        });
    zoomBehaviorRef.current = z;
    const svg = select(svgRef.current);
    svg.call(z);
    // Start at identity — the viewBox is already framing the content.
    svg.call(z.transform, zoomIdentity);
  }, [root, theme, setSelectedLifeline]);

  // Live-leased OR selected highlight: light the leaf + every
  // directory on the path back to root, dim everything else. Live
  // takes priority over selection when both are active.
  useEffect(() => {
    const root_ = innerGRef.current;
    if (!root_) return;
    const liveSet = new Set(liveLeasedPaths);
    const liveFocus = liveSet.size > 0;

    // Look up the path for the selected lifeline by walking the data.
    let selectedPath: string | null = null;
    if (selectedLifeline) {
      root_.querySelectorAll<SVGGElement>("g.node").forEach((g) => {
        const datum = (g as any).__data__?.data?.node;
        if (datum?.id === selectedLifeline) {
          selectedPath = g.getAttribute("data-path") || null;
        }
      });
    }
    const selectionFocus = !liveFocus && selectedPath != null;

    // Build the lit-path set (every ancestor directory of every
    // focal path, including the root and the file itself).
    const litPathSet = new Set<string>();
    const focalPaths: string[] = [];
    if (liveFocus)      for (const p of liveSet) focalPaths.push(p);
    if (selectionFocus) focalPaths.push(selectedPath!);
    if (focalPaths.length > 0) litPathSet.add("");      // root
    for (const p of focalPaths) {
      const parts = p.split("/").filter(Boolean);
      let accum = "";
      for (let i = 0; i < parts.length; i++) {
        accum = accum ? accum + "/" + parts[i] : parts[i];
        litPathSet.add(accum);
      }
    }
    const anyFocus = liveFocus || selectionFocus;

    root_.querySelectorAll<SVGGElement>("g.node").forEach((g) => {
      const p = g.getAttribute("data-path") || "";
      const kind = g.getAttribute("data-kind") || "";
      const datum = (g as any).__data__?.data?.node;
      const isLive = liveSet.has(p);
      const isSelectedLifeline = !!datum?.id && datum?.id === selectedLifeline;
      const isSelectedDir = kind === "dir" && selectedDirectory === p + "/";
      const isSelected = isSelectedLifeline || isSelectedDir ||
                          (kind === "root" && selectedDirectory === "@project");
      const isFocal = isLive || isSelected;
      const isLit   = litPathSet.has(p);
      const isHovered = hoveredPath === p && p !== "";

      if (anyFocus) {
        g.style.opacity = isFocal ? "1" : (isLit ? "0.95" : "0.06");
      } else {
        g.style.opacity = "1";
      }

      const circle = g.querySelector<SVGCircleElement>("circle");
      if (!circle) return;
      if (isLive || isSelected) {
        circle.setAttribute("r", "7");
        circle.setAttribute("stroke", "#2563eb");
        circle.setAttribute("stroke-width", "3");
      } else if (isHovered) {
        circle.setAttribute("r", kind === "file" ? "4.5" : "5");
        circle.setAttribute("stroke", "#2563eb");
        circle.setAttribute("stroke-width", "1.5");
      } else {
        circle.setAttribute(
          "r", kind === "root" ? "5" : kind === "dir" ? "4" : "3.5");
        circle.setAttribute("stroke", "#e5e5e5");
        circle.setAttribute("stroke-width", "1");
      }

      // Label visibility:
      //   directory labels are STRUCTURAL — always show (they're the
      //   tree's navigational signal)
      //   file labels are leaves — only show on hover / focal / lit /
      //   at 1.2× zoom, since they outnumber directories ~5:1 on a
      //   real repo and rendering all of them stalls big projects.
      const label = g.querySelector<SVGTextElement>("text.node-label");
      if (label) {
        if (kind === "dir" || kind === "root") {
          label.style.display = "";
        } else {
          const show = zoomLevel >= 1.2 || isFocal || isLit || isHovered;
          label.style.display = show ? "" : "none";
        }
      }
    });

    root_.querySelectorAll<SVGPathElement>("g.edges path").forEach((l) => {
      const s = l.getAttribute("data-source-path") || "";
      const t = l.getAttribute("data-target-path") || "";
      const onLit = anyFocus && litPathSet.has(s) && litPathSet.has(t);
      l.style.opacity = anyFocus ? (onLit ? "0.9" : "0.04") : "0.4";
      l.setAttribute("stroke-width", onLit ? "2.5" : "1");
    });
  }, [liveLeasedPaths, selectedLifeline, selectedDirectory, hoveredPath, zoomLevel, root]);

  // Pan to the selected node in schema view — same fix shape as the
  // force-directed graph. Scrolls the selected leaf or directory
  // into the visible area when the operator clicks something in
  // tree, then switches to schema.
  useEffect(() => {
    if (!svgRef.current || !zoomBehaviorRef.current) return;
    const inner = innerGRef.current;
    if (!inner) return;
    // Find the rendered <g.node> for the current selection (lifeline
    // OR directory). We don't have node coords in JS state — they
    // live on the SVG attribute — so we parse them out of the
    // node's transform string.
    const allNodes = Array.from(inner.querySelectorAll<SVGGElement>("g.node"));
    const target = allNodes.find((g) => {
      const datum = (g as any).__data__?.data;
      if (!datum) return false;
      if (selectedLifeline && datum.node?.id === selectedLifeline) return true;
      if (selectedDirectory) {
        if (datum.kind === "dir" && datum.path + "/" === selectedDirectory) return true;
        if (datum.kind === "root" && selectedDirectory === "@project") return true;
      }
      return false;
    });
    if (!target) return;
    const transform = target.getAttribute("transform") || "";
    const m = transform.match(/translate\(([-\d.]+),\s*([-\d.]+)\)/);
    if (!m) return;
    const x = parseFloat(m[1]), y = parseFloat(m[2]);
    const targetT = zoomIdentity.scale(1.4).translate(-x, -y);
    select(svgRef.current).call(
      zoomBehaviorRef.current.transform, targetT,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedLifeline, selectedDirectory, rawNodes]);

  return (
    <div className="relative h-full w-full bg-bg overflow-hidden">
      <svg ref={svgRef} className="absolute inset-0 w-full h-full text-ink">
        <g ref={innerGRef} />
      </svg>
      <div className="absolute top-2 right-2 flex flex-col gap-1 items-end">
        <div className="flex items-center gap-2 rounded-md bg-elev/95 border border-line px-2 py-1 text-xs shadow-soft">
          <span className="text-muted text-[10px]">show:</span>
          <div className="inline-flex rounded border border-line overflow-hidden">
            {(["all", "dirs", "files"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setNodeLevel(v)}
                className={`px-1.5 py-0.5 text-[10px] ${
                  nodeLevel === v
                    ? "bg-accent text-accent-fg"
                    : "text-muted hover:bg-sunken"
                }`}
              >{v}</button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2 rounded-md bg-elev/95 border border-line px-2 py-1 text-xs shadow-soft">
          <label className="flex items-center gap-1 cursor-pointer"
                 title="tombstoned lifelines — files deleted via `projmem deleting`, kept queryable forever">
            <input type="checkbox" checked={showGhosts}
                   onChange={(e) => setShowGhosts(e.target.checked)} />
            <span>ghosts ⓘ</span>
          </label>
          <span className="text-muted">·</span>
          <span className="font-mono text-muted tabular-nums">
            {zoomLevel.toFixed(1)}×
          </span>
        </div>
      </div>
      {rawNodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-muted">
          no lifelines yet — `projmem index` to populate
        </div>
      )}
      <div className="absolute bottom-2 left-2 rounded-md bg-elev/95 border border-line px-2 py-1 text-[10px] text-muted shadow-soft">
        scroll to zoom · click leaf → file · click dir → "why does this exist?"<br />
        labels hidden below 1.5× — hover or zoom to reveal · Esc to deselect
      </div>
    </div>
  );
}
