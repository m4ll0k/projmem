import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { GraphNode, GraphPayload } from "../types";

// ── Tree shape ────────────────────────────────────────────────────────────
// Build a directory tree from a flat list of GraphNodes. Each interior
// node holds a sorted map of child dirs + sorted files. Folder paths
// are the join of every key down to the node so the tree's stable
// across refetches (same input order → same output position).

interface DirNode {
  name:     string;             // last segment ("auth"), or "" at root
  fullPath: string;             // "src/auth", "" at root
  dirs:     Map<string, DirNode>;
  files:    GraphNode[];        // sorted by basename
}

function buildTree(nodes: GraphNode[]): DirNode {
  const root: DirNode = { name: "", fullPath: "", dirs: new Map(), files: [] };
  for (const n of nodes) {
    if (n.symbol) continue; // graph payload may include symbol nodes; skip
    const path = n.path ?? "";
    const parts = path.split("/").filter(Boolean);
    if (parts.length === 0) continue;
    const dirParts = parts.slice(0, -1);
    let cur = root;
    let accum = "";
    for (const p of dirParts) {
      accum = accum ? accum + "/" + p : p;
      let child = cur.dirs.get(p);
      if (!child) {
        child = { name: p, fullPath: accum, dirs: new Map(), files: [] };
        cur.dirs.set(p, child);
      }
      cur = child;
    }
    cur.files.push(n);
  }
  const sort = (d: DirNode) => {
    d.files.sort((a, b) => (a.path || "").localeCompare(b.path || ""));
    d.dirs = new Map([...d.dirs.entries()].sort((a, b) =>
      a[0].localeCompare(b[0])));
    for (const child of d.dirs.values()) sort(child);
  };
  sort(root);
  return root;
}

// ── Visual encoding ───────────────────────────────────────────────────────

const STALE_LABEL: Record<string, string> = {
  contradicted:   "REFUTED",
  strongly_stale: "STALE",
  weakly_stale:   "soft-stale",
};

function NodeBadges({ n, live }: { n: GraphNode; live: boolean }) {
  return (
    <span className="flex items-center gap-1 ml-2">
      {live && (
        <span className="px-1 py-px text-[9px] font-medium rounded
                          bg-accent/15 text-accent border border-accent/30
                          animate-pulse">
          editing
        </span>
      )}
      {n.critical && (
        <span className="px-1 py-px text-[9px] font-medium rounded
                          bg-bad/15 text-bad border border-bad/30"
              title="critical note attached">
          ⚠ critical
        </span>
      )}
      {n.leased && !live && (
        <span className="px-1 py-px text-[9px] font-medium rounded
                          bg-accent/10 text-accent border border-accent/20">
          leased
        </span>
      )}
      {STALE_LABEL[n.staleness] && (
        <span className={`px-1 py-px text-[9px] font-medium rounded border ${
          n.staleness === "contradicted"
            ? "bg-bad/15 text-bad border-bad/30"
            : "bg-warn/15 text-warn border-warn/30"
        }`}>
          {STALE_LABEL[n.staleness]}
        </span>
      )}
      {n.rev_deps > 0 && (
        <span className="text-[9px] font-mono text-muted">
          ←{n.rev_deps}
        </span>
      )}
    </span>
  );
}

// ── Row + recursive renderer ──────────────────────────────────────────────

function FileRow({ n, live, selected, onClick }: {
  n: GraphNode;
  live: boolean;
  selected: boolean;
  onClick: () => void;
}) {
  const base = (n.path || "").split("/").pop() || "—";
  return (
    <div
      data-tree-lifeline={n.id}
      onClick={onClick}
      className={`flex items-center px-2 py-1 text-xs cursor-pointer
        rounded-sm transition-colors
        ${selected
          ? "bg-accent/15 text-accent border-l-2 border-accent pl-[6px]"
          : live
            ? "bg-accent/5 text-ink"
            : "text-ink hover:bg-sunken"}`}
    >
      <span className="font-mono text-muted text-[10px] w-3 mr-1">·</span>
      <span className="font-mono truncate flex-1">{base}</span>
      <NodeBadges n={n} live={live} />
    </div>
  );
}

function DirSection({ dir, depth, liveSet, selectedLifeline,
                      selectedDirectory,
                      onSelect, onSelectDir,
                      collapsed, toggle }: {
  dir: DirNode;
  depth: number;
  liveSet: Set<string>;
  selectedLifeline: string | null;
  selectedDirectory: string | null;
  onSelect: (lifelineId: string) => void;
  onSelectDir: (target: string) => void;
  collapsed: Set<string>;
  toggle: (path: string) => void;
}) {
  const isCollapsed = collapsed.has(dir.fullPath);
  // Surface live + critical counts on the directory chip so the user
  // can see at a glance which subtrees have action.
  const liveInTree = useMemo(() => {
    let n = 0;
    const walk = (d: DirNode) => {
      for (const f of d.files) {
        if (f.path && liveSet.has(f.path)) n++;
      }
      for (const c of d.dirs.values()) walk(c);
    };
    walk(dir);
    return n;
  }, [dir, liveSet]);
  const criticalInTree = useMemo(() => {
    let n = 0;
    const walk = (d: DirNode) => {
      for (const f of d.files) if (f.critical) n++;
      for (const c of d.dirs.values()) walk(c);
    };
    walk(dir);
    return n;
  }, [dir]);

  const isSelectedDir = selectedDirectory === dir.fullPath + "/";
  return (
    <div className="select-none">
      {dir.fullPath !== "" && (
        <div
          data-tree-dir={dir.fullPath + "/"}
          style={{ paddingLeft: `${depth * 12 + 6}px` }}
          className={`flex items-center px-2 py-1 text-[11px] rounded-sm
                     ${isSelectedDir
                        ? "bg-accent/15 text-accent border-l-2 border-accent"
                        : "text-ink hover:bg-sunken"}`}
        >
          {/* Triangle = collapse/expand; separate click target from
              the directory name so the operator can "select the dir
              as a target" without also collapsing/expanding it. */}
          <span
            onClick={(e) => { e.stopPropagation(); toggle(dir.fullPath); }}
            className="font-mono text-muted text-[10px] w-3 mr-1 cursor-pointer hover:text-ink"
            title={isCollapsed ? "expand" : "collapse"}
          >
            {isCollapsed ? "▶" : "▼"}
          </span>
          {/* Name = "select this dir as the inspector target". */}
          <span
            onClick={() => onSelectDir(dir.fullPath + "/")}
            className="font-mono cursor-pointer flex-1 hover:underline"
            title={`open ${dir.fullPath}/ in the inspector — add notes / guidance about why this directory exists`}
          >
            📁 {dir.name}
          </span>
          <span className="ml-auto flex items-center gap-1">
            {liveInTree > 0 && (
              <span className="px-1 text-[9px] rounded bg-accent/15
                                text-accent border border-accent/30 animate-pulse">
                {liveInTree} live
              </span>
            )}
            {criticalInTree > 0 && (
              <span className="px-1 text-[9px] rounded bg-bad/15
                                text-bad border border-bad/30">
                {criticalInTree} ⚠
              </span>
            )}
          </span>
        </div>
      )}
      {!isCollapsed && (
        <div style={{ paddingLeft: dir.fullPath === ""
                                    ? "0"
                                    : `${(depth + 1) * 12 + 6}px` }}>
          {dir.files.map((n) => (
            <FileRow
              key={n.id}
              n={n}
              live={!!(n.path && liveSet.has(n.path))}
              selected={selectedLifeline === n.id}
              onClick={() => onSelect(n.id)}
            />
          ))}
          {[...dir.dirs.values()].map((sub) => (
            <DirSection
              key={sub.fullPath}
              dir={sub}
              depth={depth + 1}
              liveSet={liveSet}
              selectedLifeline={selectedLifeline}
              selectedDirectory={selectedDirectory}
              onSelect={onSelect}
              onSelectDir={onSelectDir}
              collapsed={collapsed}
              toggle={toggle}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────────

export function TreeView() {
  const showGhosts           = useStore((s) => s.showGhosts);
  const setShowGhosts        = useStore((s) => s.setShowGhosts);
  const nodeLevel            = useStore((s) => s.nodeLevel);
  const setNodeLevel         = useStore((s) => s.setNodeLevel);
  const liveLeasedPaths      = useStore((s) => s.liveLeasedPaths);
  const selectedLifeline     = useStore((s) => s.selectedLifeline);
  const selectedDirectory    = useStore((s) => s.selectedDirectory);
  const setSelectedLifeline  = useStore((s) => s.setSelectedLifeline);
  const setSelectedDirectory = useStore((s) => s.setSelectedDirectory);
  const events               = useStore((s) => s.events);

  const [payload, setPayload] = useState<GraphPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter]   = useState("");
  const [refetchTick, setRefetchTick] = useState(0);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());

  // Same auto-refetch trick as the graph — structural events bump the tick.
  useEffect(() => {
    const lastFew = events.slice(-10);
    const structural = lastFew.some((e) =>
      e.kind === "created" || e.kind === "deleted" || e.kind === "moved");
    if (!structural) return;
    const t = setTimeout(() => setRefetchTick((n) => n + 1), 400);
    return () => clearTimeout(t);
  }, [events.length]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.graph(showGhosts, false)
      .then((g) => !cancelled && setPayload(g))
      .catch(() => { /* ignore */ })
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [showGhosts, refetchTick]);

  const tree = useMemo(() => {
    const nodes = payload?.nodes ?? [];
    const q = filter.trim().toLowerCase();
    const filtered = q
      ? nodes.filter((n) => (n.path || "").toLowerCase().includes(q))
      : nodes;
    const full = buildTree(filtered);
    if (nodeLevel === "files") return full;
    if (nodeLevel === "dirs") {
      // Show directory structure only — drop file rows but keep the
      // dir headers so the operator can click them.
      const stripFiles = (d: DirNode): DirNode => ({
        ...d,
        files: [],
        dirs:  new Map([...d.dirs.entries()].map(
          ([k, v]) => [k, stripFiles(v)],
        )),
      });
      return stripFiles(full);
    }
    return full;
  }, [payload, filter, nodeLevel]);

  const liveSet = useMemo(() => new Set(liveLeasedPaths), [liveLeasedPaths]);

  const toggle = (p: string) => setCollapsed((s) => {
    const next = new Set(s);
    if (next.has(p)) next.delete(p);
    else next.add(p);
    return next;
  });

  const totalFiles = payload?.nodes?.length ?? 0;
  const liveCount  = liveLeasedPaths.length;

  // When selection changes (from any source), auto-expand the
  // selected file/dir's ancestor directories AND scroll the
  // corresponding row into view. Wire up: selecting a file in graph
  // → switch to tree → the row is already on screen and highlighted.
  useEffect(() => {
    const lifelinePath = payload?.nodes?.find(
      (n) => n.id === selectedLifeline)?.path;
    const target = selectedDirectory
                ?? (lifelinePath ? lifelinePath : null);
    if (!target) return;
    // Expand every ancestor directory that's currently collapsed.
    const parts = target.replace(/\/$/, "").split("/").filter(Boolean);
    let accum = "";
    setCollapsed((s) => {
      const next = new Set(s);
      for (let i = 0; i < parts.length - 1; i++) {
        accum = accum ? accum + "/" + parts[i] : parts[i];
        next.delete(accum);
      }
      return next;
    });
    // Scroll into view after the DOM has had a tick to render the
    // expanded rows.
    const id = setTimeout(() => {
      const sel = selectedDirectory
        ? `[data-tree-dir="${selectedDirectory}"]`
        : `[data-tree-lifeline="${selectedLifeline}"]`;
      const el = document.querySelector<HTMLElement>(sel);
      el?.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 50);
    return () => clearTimeout(id);
  }, [selectedLifeline, selectedDirectory, payload?.nodes]);

  return (
    <div className="h-full w-full flex flex-col bg-bg">
      <div className="border-b border-line px-3 py-2 flex items-center gap-2 flex-wrap">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="filter paths…"
          className="flex-1 min-w-[100px] text-xs bg-bg border border-line rounded px-2 py-1
                     text-ink focus:outline-none focus:border-accent h-7"
        />
        <div className="inline-flex rounded border border-line overflow-hidden">
          {(["all", "dirs", "files"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setNodeLevel(v)}
              title={
                v === "all"   ? "show directories + files"   :
                v === "dirs"  ? "show only the directory structure (skip individual files)" :
                                "show only files (skip directory headers)"
              }
              className={`px-1.5 py-0.5 text-[10px] ${
                nodeLevel === v
                  ? "bg-accent text-accent-fg"
                  : "text-muted hover:bg-sunken"
              }`}
            >{v}</button>
          ))}
        </div>
        <label className="flex items-center gap-1 text-xs text-muted cursor-pointer"
               title="Tombstoned lifelines — files deleted via `projmem deleting`. Their identity + every saved note + every history event is kept forever; toggling this on surfaces them as faded entries with a `replaced_by` link to their successor.">
          <input type="checkbox" checked={showGhosts}
                 onChange={(e) => setShowGhosts(e.target.checked)} />
          ghosts ⓘ
        </label>
        <span className="text-[10px] font-mono text-muted tabular-nums">
          {totalFiles}{liveCount > 0 && (
            <> · <span className="text-accent">{liveCount} live</span></>
          )}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto py-1">
        {loading && payload === null && (
          <div className="text-xs text-muted px-3 py-2">loading…</div>
        )}
        {payload !== null && tree.files.length === 0 && tree.dirs.size === 0 && (
          <div className="text-xs text-muted px-3 py-2">
            no files match filter
          </div>
        )}
        <DirSection
          dir={tree} depth={0}
          liveSet={liveSet}
          selectedLifeline={selectedLifeline}
          selectedDirectory={selectedDirectory}
          onSelect={setSelectedLifeline}
          onSelectDir={setSelectedDirectory}
          collapsed={collapsed}
          toggle={toggle}
        />
      </div>
    </div>
  );
}
