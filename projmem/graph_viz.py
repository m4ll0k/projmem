"""projmem/graph_viz.py — claim-and-drift-aware graph visualization.

The wedge artifact: a graph where nodes are colored by whether the
attached claims still hold against the current code, and a black ring
flags files that drifted on disk since the last verify. Renders to
DOT, Mermaid, and (when graphviz is available) SVG.

Public API:
  - build_graph_data(store, repo_root, target=None, hops=2,
                     include_files=True, full=False) -> dict
  - render_dot(data) -> str
  - render_mermaid(data) -> str
  - render_svg(dot_str) -> bytes | None  (None when graphviz missing)
  - write_outputs(out_dir, data, formats=("dot","mmd","svg")) -> dict

The "graph" is structural (edges in the projmem index) plus a CLAIM
overlay: each node carries a status label that's the reduction of all
annotations whose target matches it.
"""
from __future__ import annotations
import os
import shutil
import subprocess
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

from . import render as _render


# Edge types we know how to draw. Anything unknown still renders, just
# without a colored label.
EDGE_TYPE_LABEL = {
    "imports":       "import",
    "reexport_star": "reexport*",
    "extends":       "extends",
    "implements":    "implements",
    "calls":         "calls",
}

# Maximum nodes drawn before we truncate. SVGs above this become
# unreadable thumbnails — better to refuse than mislead.
MAX_NODES_DEFAULT = 150


def build_graph_data(store, repo_root: str,
                     target: Optional[str] = None,
                     hops: int = 2,
                     include_files: bool = True,
                     full: bool = False,
                     max_nodes: int = MAX_NODES_DEFAULT,
                     ) -> Dict[str, Any]:
    """Build the graph payload for renderers.

    target=None + full=True   → whole repo (capped at max_nodes)
    target=<file or symbol>   → 2-hop neighborhood (or `hops`-hop)
    target=<bare symbol name> → resolved to its def file, then BFS

    Returns:
      {
        target, hops, full, truncated,
        nodes: [{id, label, kind, file, status, refcount, drifted}],
        edges: [{src, dst, type, confidence, style}],
        legend: {colors, edges},
        stats: {node_count, edge_count, refuted_nodes, drifted_nodes},
      }
    """
    seed_files, seed_symbols = _resolve_seed(store, target, full)

    # BFS over the structural edge graph.
    file_nodes: Set[str] = set(seed_files)
    sym_nodes: Set[Tuple[str, str]] = set()  # (file, name)
    edges_seen: Set[Tuple[str, str, str]] = set()
    edges_out: List[Dict[str, Any]] = []
    truncated = False

    if full:
        # Whole-repo mode — pull every edge whose endpoints map to a file.
        for r in store.conn.execute("SELECT * FROM edges"):
            edge_dict = _edge_dict(r)
            if not edge_dict:
                continue
            if edge_dict["src"] not in file_nodes and len(file_nodes) >= max_nodes:
                truncated = True
                continue
            file_nodes.add(edge_dict["src"])
            file_nodes.add(edge_dict["dst"])
            key = (edge_dict["src"], edge_dict["dst"], edge_dict["type"])
            if key in edges_seen:
                continue
            edges_seen.add(key)
            edges_out.append(edge_dict)
            if len(edges_out) >= max_nodes * 4:
                truncated = True
                break
    else:
        frontier: Deque[Tuple[str, int]] = deque((p, 0) for p in seed_files)
        visited: Set[str] = set()
        while frontier:
            node, depth = frontier.popleft()
            if node in visited:
                continue
            visited.add(node)
            file_nodes.add(node)
            if len(file_nodes) >= max_nodes:
                truncated = True
                break
            if depth >= hops:
                continue
            for r in store.edges_from(node):
                edge_dict = _edge_dict(r)
                if not edge_dict:
                    continue
                key = (edge_dict["src"], edge_dict["dst"], edge_dict["type"])
                if key not in edges_seen:
                    edges_seen.add(key)
                    edges_out.append(edge_dict)
                if edge_dict["dst"] and not _is_external(edge_dict["dst"]):
                    frontier.append((edge_dict["dst"], depth + 1))
            for r in store.edges_to(node):
                edge_dict = _edge_dict(r)
                if not edge_dict:
                    continue
                key = (edge_dict["src"], edge_dict["dst"], edge_dict["type"])
                if key not in edges_seen:
                    edges_seen.add(key)
                    edges_out.append(edge_dict)
                if edge_dict["src"] and not _is_external(edge_dict["src"]):
                    frontier.append((edge_dict["src"], depth + 1))

    # Surface seeded symbols (when target was a `file#name` or bare symbol).
    for f, n in seed_symbols:
        sym_nodes.add((f, n))
        file_nodes.add(f)

    # Pull symbol nodes for the seed files when requested.
    if include_files and not full:
        # Add top symbols per file so the graph isn't just a file blob.
        for f in list(file_nodes):
            if _is_external(f):
                continue
            for r in store.conn.execute(
                    "SELECT name, kind FROM symbols WHERE file=? "
                    "ORDER BY exported DESC, name LIMIT 6", (f,)):
                sym_nodes.add((f, r["name"]))

    # Drift overlay — query once per file_set.
    drifted = _render.drifted_paths(store, repo_root, file_nodes)

    # Build node list with claim overlay.
    nodes: List[Dict[str, Any]] = []
    refuted_count = 0
    drifted_count = 0
    for f in sorted(file_nodes):
        if not f:
            continue
        status = _render.status_for_target(store, repo_root, f, persist=False)
        is_drifted = f in drifted
        if status["label"] == "REFUTED":
            refuted_count += 1
        if is_drifted:
            drifted_count += 1
        nodes.append({
            "id":       _node_id("file", f),
            "label":    _short_path(f),
            "kind":     "file",
            "file":     f,
            "status":   status["label"],
            "claims":   {
                "verified": status["verified_claims"],
                "refuted":  status["refuted_claims"],
                "notes":    status["note_count"],
            },
            "refcount": 0,
            "drifted":  is_drifted,
        })
    for f, n in sorted(sym_nodes):
        target_str = f"{f}#{n}"
        status = _render.status_for_target(store, repo_root, target_str,
                                           persist=False)
        # Fall back to bare-name target if the file#name target had nothing.
        if status["label"] == "NONE":
            bare = _render.status_for_target(store, repo_root, n,
                                             persist=False)
            if bare["label"] != "NONE":
                status = bare
        rc = _render.refcount_for_symbol(store, n)
        if status["label"] == "REFUTED":
            refuted_count += 1
        nodes.append({
            "id":       _node_id("sym", target_str),
            "label":    n,
            "kind":     "symbol",
            "file":     f,
            "status":   status["label"],
            "claims":   {
                "verified": status["verified_claims"],
                "refuted":  status["refuted_claims"],
                "notes":    status["note_count"],
            },
            "refcount": rc,
            "drifted":  f in drifted,
        })

    return {
        "target":     target,
        "hops":       hops,
        "full":       full,
        "truncated":  truncated,
        "nodes":      nodes,
        "edges":      edges_out,
        "legend": {
            "colors": {
                "PROVED":    _render.COLOR_PROVED,
                "REFUTED":   _render.COLOR_REFUTED,
                "AMBIGUOUS": _render.COLOR_AMBIGUOUS,
                "NONE":      _render.COLOR_NONE,
            },
            "edges": {
                "high":      "solid (EXTRACTED)",
                "medium":    "dashed (INFERRED)",
                "low":       "dotted (AMBIGUOUS)",
            },
        },
        "stats": {
            "node_count":    len(nodes),
            "edge_count":    len(edges_out),
            "refuted_nodes": refuted_count,
            "drifted_nodes": drifted_count,
        },
    }


# ---------------------------------------------------------------------------
# DOT output (graphviz source).
# ---------------------------------------------------------------------------

def render_dot(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    title = _title_for(data)
    lines.append(f'digraph projmem {{')
    lines.append(f'  graph [rankdir=LR, label="{_dot_escape(title)}", '
                 f'labelloc=t, fontname="Helvetica", fontsize=11];')
    lines.append('  node  [shape=box, style="rounded,filled", '
                 'fontname="Helvetica", fontsize=10];')
    lines.append('  edge  [fontname="Helvetica", fontsize=8];')
    for n in data["nodes"]:
        fill = _render.color_for_label(n["status"])
        bucket = _render.refcount_bucket(n.get("refcount", 0))
        w, h = _render.NODE_SIZE[bucket]
        peripheries = 2 if n["drifted"] else 1
        ring = _render.COLOR_DRIFT_RING if n["drifted"] else "#444444"
        # Badge with claim count when present.
        c = n.get("claims") or {}
        badge = ""
        if c.get("notes"):
            badge = f"  [{c.get('verified', 0)}V/{c.get('refuted', 0)}R]"
        label = f"{n['label']}{badge}"
        if n["kind"] == "symbol":
            label = f"sym {label}"
        shape = "box" if n["kind"] == "file" else "ellipse"
        lines.append(
            f'  "{n["id"]}" [label="{_dot_escape(label)}", '
            f'fillcolor="{fill}", color="{ring}", penwidth=2, '
            f'peripheries={peripheries}, shape={shape}, '
            f'width={w:.2f}, height={h:.2f}];'
        )
    for e in data["edges"]:
        style = _render.edge_style_for_confidence(e.get("confidence"))
        et = e.get("type") or ""
        label = EDGE_TYPE_LABEL.get(et, et)
        src_id = _node_id("file", e["src"])
        dst_id = _node_id("file", e["dst"])
        # Skip edges where neither endpoint is in the node set.
        node_ids = {n["id"] for n in data["nodes"]}
        if src_id not in node_ids and dst_id not in node_ids:
            continue
        lines.append(
            f'  "{src_id}" -> "{dst_id}" '
            f'[style={style}, label="{_dot_escape(label)}"];'
        )
    lines.append('}')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Mermaid output (markdown-embeddable).
# ---------------------------------------------------------------------------

def render_mermaid(data: Dict[str, Any]) -> str:
    """Mermaid graph syntax — embeddable in GitHub markdown without a
    graphviz install. Loses the ring (Mermaid has no per-node border
    color), so drift is encoded in the node label as `[DRIFT]`."""
    lines: List[str] = []
    lines.append("```mermaid")
    lines.append("graph LR")
    # Class definitions for the four status buckets.
    lines.append(f"  classDef proved fill:{_render.COLOR_PROVED},stroke:#222,color:#000;")
    lines.append(f"  classDef refuted fill:{_render.COLOR_REFUTED},stroke:#000,color:#fff,stroke-width:2px;")
    lines.append(f"  classDef ambiguous fill:{_render.COLOR_AMBIGUOUS},stroke:#222,color:#000;")
    lines.append(f"  classDef none fill:{_render.COLOR_NONE},stroke:#444,color:#000;")
    lines.append(f"  classDef drift stroke:#000,stroke-width:3px,stroke-dasharray:5 5;")
    for n in data["nodes"]:
        nid = _mermaid_id(n["id"])
        c = n.get("claims") or {}
        badge = f" [{c.get('verified', 0)}V/{c.get('refuted', 0)}R]" \
            if c.get("notes") else ""
        drift_tag = " [DRIFT]" if n["drifted"] else ""
        label = f"{n['label']}{badge}{drift_tag}"
        shape_open, shape_close = ("[", "]") if n["kind"] == "file" else ("([", "])")
        lines.append(f'  {nid}{shape_open}"{_mermaid_escape(label)}"{shape_close}')
        cls = {
            "PROVED":    "proved",
            "REFUTED":   "refuted",
            "AMBIGUOUS": "ambiguous",
        }.get(n["status"], "none")
        lines.append(f"  class {nid} {cls};")
        if n["drifted"]:
            lines.append(f"  class {nid} drift;")
    for e in data["edges"]:
        node_ids = {n["id"] for n in data["nodes"]}
        src_id = _node_id("file", e["src"])
        dst_id = _node_id("file", e["dst"])
        if src_id not in node_ids and dst_id not in node_ids:
            continue
        et = EDGE_TYPE_LABEL.get(e.get("type") or "", e.get("type") or "")
        arrow = _mermaid_arrow(e.get("confidence"))
        lines.append(f"  {_mermaid_id(src_id)} {arrow}|{et}| {_mermaid_id(dst_id)}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _mermaid_arrow(conf: Optional[str]) -> str:
    style = _render.edge_style_for_confidence(conf)
    if style == "dashed":
        return "-.->"
    if style == "dotted":
        return "-.->"
    return "-->"


# ---------------------------------------------------------------------------
# SVG output (graphviz binary required).
# ---------------------------------------------------------------------------

def render_svg(dot_str: str) -> Optional[bytes]:
    """Convert a DOT source string into SVG bytes by piping it through
    the system `dot` binary. Returns None when graphviz is not installed
    so the caller can fall back to writing only DOT/MMD."""
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return None
    try:
        proc = subprocess.run(
            [dot_bin, "-Tsvg"],
            input=dot_str.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


# ---------------------------------------------------------------------------
# Filesystem orchestration.
# ---------------------------------------------------------------------------

def write_outputs(out_dir: str, data: Dict[str, Any],
                  formats: Iterable[str] = ("dot", "mmd", "svg")
                  ) -> Dict[str, Any]:
    """Write the requested formats into `out_dir` as `graph.<ext>`.
    Always writes DOT if requested. SVG is skipped (silently) when
    graphviz isn't installed — the dot file remains the source of
    truth and the user can render externally.
    """
    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    skipped: List[Dict[str, str]] = []
    formats = set(formats)

    dot = render_dot(data)
    if "dot" in formats:
        path = os.path.join(out_dir, "graph.dot")
        with open(path, "w", encoding="utf-8") as f:
            f.write(dot)
        written.append(path)
    if "mmd" in formats:
        path = os.path.join(out_dir, "graph.mmd")
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_mermaid(data))
        written.append(path)
    if "svg" in formats:
        svg = render_svg(dot)
        if svg is None:
            skipped.append({
                "path":   os.path.join(out_dir, "graph.svg"),
                "reason": ("graphviz `dot` binary not found on PATH. "
                           "Install with: brew install graphviz / "
                           "apt-get install graphviz / "
                           "pacman -S graphviz."),
            })
        else:
            path = os.path.join(out_dir, "graph.svg")
            with open(path, "wb") as f:
                f.write(svg)
            written.append(path)
    return {"wrote": written, "skipped": skipped}


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

def _resolve_seed(store, target: Optional[str], full: bool
                  ) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    """Resolve the user's target string into seed file/symbol nodes."""
    if full or not target:
        return set(), set()
    # `file#name` shorthand — split into file + symbol seeds.
    if "#" in target:
        f, n = target.split("#", 1)
        return ({f}, {(f, n)})
    # Bare path that exists in the index → file seed.
    row = store.conn.execute(
        "SELECT 1 FROM files WHERE path=? LIMIT 1", (target,)).fetchone()
    if row is not None:
        return ({target}, set())
    # Bare symbol name → look up def(s).
    rows = store.symbols_by_name(target)
    if rows:
        return ({r["file"] for r in rows},
                {(r["file"], r["name"]) for r in rows})
    # Last resort: treat the string as a file path so the renderer at
    # least surfaces "no nodes found".
    return ({target}, set())


def _edge_dict(row) -> Optional[Dict[str, Any]]:
    """Row → dict, dropping rows whose endpoints are non-graphable."""
    src = row["src"] if "src" in row.keys() else None
    dst = row["dst"] if "dst" in row.keys() else None
    if not src or not dst:
        return None
    # Skip purely external destinations from the BFS but keep them in
    # the rendered set so callers can see the edge.
    return {
        "src":         src,
        "dst":         dst,
        "type":        row["type"] if "type" in row.keys() else "",
        "confidence":  row["confidence"] if "confidence" in row.keys() else "",
    }


def _is_external(s: str) -> bool:
    """Heuristic: is this edge endpoint outside the source graph?"""
    if not s:
        return True
    return (s.startswith("module:")
            or s.startswith("builtin:")
            or s.startswith("binding:"))


def _title_for(data: Dict[str, Any]) -> str:
    if data.get("full"):
        scope = "whole repo"
    elif data.get("target"):
        scope = f"{data['target']} (±{data['hops']} hops)"
    else:
        scope = "neighborhood"
    s = data.get("stats") or {}
    return (f"projmem graph — {scope} · "
            f"{s.get('node_count', 0)} nodes · "
            f"{s.get('refuted_nodes', 0)} REFUTED · "
            f"{s.get('drifted_nodes', 0)} drifted")


def _short_path(path: str) -> str:
    """Trim long paths so they fit in node labels."""
    if len(path) <= 40:
        return path
    parts = path.split("/")
    if len(parts) <= 2:
        return path
    return ".../" + "/".join(parts[-2:])


def _node_id(prefix: str, value: str) -> str:
    """Stable, DOT-safe node id."""
    safe = "".join(c if c.isalnum() else "_" for c in (value or ""))
    return f"{prefix}_{safe}"


def _mermaid_id(s: str) -> str:
    """Mermaid is stricter about ids than DOT — strip everything but
    alnum and underscore, ensure leading char is a letter."""
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in s)
    if not out or not out[0].isalpha():
        out = "n_" + out
    return out


def _dot_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n"))


def _mermaid_escape(s: str) -> str:
    return s.replace('"', "'").replace("\n", " ")
