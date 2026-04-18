"""projmem/mcp_server.py — expose projmem as an MCP server.

Lets Claude Code / Claude Desktop / any MCP-aware client call projmem
subcommands as native tools, no shell required. Run as:

    projmem mcp-server [--path REPO]

Then register in the client's MCP config — e.g. for Claude Desktop:

    {
      "mcpServers": {
        "projmem": {
          "command": "projmem",
          "args": ["mcp-server", "--path", "/path/to/repo"]
        }
      }
    }

Tools exposed (each takes a small JSON arg blob and returns the same
JSON the CLI subcommand would emit):

    projmem_session         — per-target / project bootstrap
    projmem_search          — cross-bucket substring search
    projmem_symbol          — lookup defs + refs by symbol name
    projmem_reverse         — reverse dependencies for a file
    projmem_forward         — forward dependencies for a file
    projmem_fact_check      — verify claims in arbitrary text
    projmem_notes           — project-wide memory summary
    projmem_note_add        — persist a new note (with optional claims)

All tools share a common `path` parameter so the server can serve
multiple repositories from one instance (most clients launch one
server per project, but the parameter is honored regardless).
"""
from __future__ import annotations
import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from . import config as config_mod
from .store import Store


def _open(path: str):
    cfg = config_mod.load(path or ".")
    os.makedirs(cfg.store_dir, exist_ok=True)
    return cfg, Store(cfg.db_path)


# ---- Tool implementations (sync, return JSON-serializable dicts) ----------

def tool_session(path: str = ".", target: Optional[str] = None,
                 max_notes: int = 10) -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        if target:
            from . import session as _s
            blob = _s.build_session(store, cfg.root, target,
                                    max_notes=max_notes)
        else:
            from . import notes_summary as _ns
            blob = _ns.build_summary(store, cfg.root,
                                     max_recent=max_notes)
            blob["mode"] = "project"
        return blob
    finally:
        store.close()


def tool_search(path: str = ".", query: str = "",
                limit: int = 25) -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        like = f"%{query}%"
        out: Dict[str, Any] = {"query": query, "limit": limit,
                                "buckets": {}}
        out["buckets"]["notes"] = [
            {"id": n.get("id"), "target": n.get("target"),
             "kind": n.get("kind"),
             "body": (n.get("body") or "")[:200]}
            for n in store.search_annotations(query)[:limit]]
        out["buckets"]["symbols"] = [
            {"name": r["name"], "file": r["file"], "kind": r["kind"],
             "line": r["line"]}
            for r in store.conn.execute(
                "SELECT name,file,kind,line FROM symbols "
                "WHERE name LIKE ? COLLATE NOCASE LIMIT ?",
                (like, limit))]
        out["buckets"]["contracts"] = [
            {"name": r["name"], "kind": r["kind"], "file": r["file"],
             "line": r["line"]}
            for r in store.conn.execute(
                "SELECT name,kind,file,line FROM contracts "
                "WHERE name LIKE ? COLLATE NOCASE LIMIT ?",
                (like, limit))]
        out["buckets"]["files"] = [
            {"path": r["path"], "lang": r["lang"]}
            for r in store.conn.execute(
                "SELECT path,lang FROM files WHERE path LIKE ? LIMIT ?",
                (like, limit))]
        out["total_hits"] = sum(len(v) for v in out["buckets"].values())
        return out
    finally:
        store.close()


def tool_symbol(path: str = ".", name: str = "") -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        defs = [dict(r) for r in store.symbols_by_name(name)]
        refs = [dict(r) for r in store.refs_by_name(name)]
        return {"name": name, "defs": defs, "refs": refs,
                "ref_count": len(refs)}
    finally:
        store.close()


def tool_reverse(path: str = ".", target: str = "") -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        from . import graph as _graph
        rd = _graph.reverse_deps(store, target)
        return {"target": target, "reverse_dependencies": rd}
    finally:
        store.close()


def tool_forward(path: str = ".", target: str = "") -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        rows = [dict(r) for r in store.edges_from(target)]
        return {"target": target,
                "direct_dependencies": [
                    {"file": r["dst"], "type": r["type"],
                     "confidence": r["confidence"],
                     "evidence": r.get("evidence")}
                    for r in rows
                ]}
    finally:
        store.close()


def tool_fact_check(path: str = ".", text: str = "") -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        from . import factcheck as _fc
        return _fc.fact_check(store, text or "", repo_root=cfg.root)
    finally:
        store.close()


def tool_notes(path: str = ".", max_recent: int = 10) -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        from . import notes_summary as _ns
        return _ns.build_summary(store, cfg.root, max_recent=max_recent)
    finally:
        store.close()


def tool_note_add(path: str = ".", target: str = "", body: str = "",
                  kind: str = "note",
                  truth_class: str = "INFERENCE",
                  confidence: float = 0.5,
                  claims: Optional[List[Dict[str, Any]]] = None,
                  author: Optional[str] = None) -> Dict[str, Any]:
    cfg, store = _open(path)
    try:
        ann_id = store.add_annotation(
            target=target, kind=kind, body=body, author=author,
            confidence=float(confidence), truth_class=truth_class,
            evidence=claims or None)
        return {"id": ann_id, "target": target, "kind": kind,
                "truth_class": truth_class}
    finally:
        store.close()


# ---- MCP server wiring ----------------------------------------------------

TOOL_TABLE = [
    ("projmem_session", tool_session,
     "Per-target or project bootstrap. With no target: project memory "
     "summary + known contracts. With target: per-target notes + "
     "integrity + neighbors + freshness.",
     {
        "type": "object",
        "properties": {
            "path":      {"type": "string", "default": "."},
            "target":    {"type": "string"},
            "max_notes": {"type": "integer", "default": 10},
        },
     }),
    ("projmem_search", tool_search,
     "Substring search across notes / symbols / contracts / file paths. "
     "Use this when you don't know the exact name yet.",
     {
        "type": "object",
        "required": ["query"],
        "properties": {
            "path":  {"type": "string", "default": "."},
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 25},
        },
     }),
    ("projmem_symbol", tool_symbol,
     "Look up a symbol by name — returns every def site and ref site.",
     {
        "type": "object",
        "required": ["name"],
        "properties": {
            "path": {"type": "string", "default": "."},
            "name": {"type": "string"},
        },
     }),
    ("projmem_reverse", tool_reverse,
     "Reverse dependencies for a file — who imports / depends on it.",
     {
        "type": "object",
        "required": ["target"],
        "properties": {
            "path":   {"type": "string", "default": "."},
            "target": {"type": "string", "description": "File path"},
        },
     }),
    ("projmem_forward", tool_forward,
     "Forward dependencies for a file — what does it import.",
     {
        "type": "object",
        "required": ["target"],
        "properties": {
            "path":   {"type": "string", "default": "."},
            "target": {"type": "string", "description": "File path"},
        },
     }),
    ("projmem_fact_check", tool_fact_check,
     "Verify inline claims (file:line citations, @predicate(subject, "
     "object) annotations) in arbitrary text against the current code "
     "index. Returns verdict, refuted/verified counts, evidence.",
     {
        "type": "object",
        "required": ["text"],
        "properties": {
            "path": {"type": "string", "default": "."},
            "text": {"type": "string"},
        },
     }),
    ("projmem_notes", tool_notes,
     "Project-wide memory summary — totals, recent notes, contradicted, "
     "risk-ranked targets. Call this at session start.",
     {
        "type": "object",
        "properties": {
            "path":       {"type": "string", "default": "."},
            "max_recent": {"type": "integer", "default": 10},
        },
     }),
    ("projmem_note_add", tool_note_add,
     "Persist a new note (annotation) on `target`. `claims` is an "
     "optional list of {subject, predicate, object, truth_class, "
     "confidence} entries that future sessions will revalidate.",
     {
        "type": "object",
        "required": ["target", "body"],
        "properties": {
            "path":         {"type": "string", "default": "."},
            "target":       {"type": "string"},
            "body":         {"type": "string"},
            "kind":         {"type": "string", "default": "note"},
            "truth_class":  {"type": "string",
                              "enum": ["FACT", "INFERENCE",
                                       "ASSUMPTION", "UNKNOWN"],
                              "default": "INFERENCE"},
            "confidence":   {"type": "number", "default": 0.5},
            "claims":       {"type": "array"},
            "author":       {"type": "string"},
        },
     }),
]


async def serve(default_path: str = ".") -> int:
    """Run the MCP server on stdio. Returns a non-zero exit code only
    on transport-level failure; tool-level errors are propagated to
    the client as MCP errors, not as process exits."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import Tool, TextContent
    except ImportError as e:
        # F020 (round-7): standardized JSON envelope to stdout +
        # exit 2, matching every other structured-error path. The
        # prior plain-text + exit 1 was both unparseable and used
        # the wrong rc convention.
        import sys as _sys
        payload = {
            "error":   "missing-dependency",
            "message": ("the `mcp` Python package is not installed; "
                         "projmem mcp-server cannot start"),
            "detail":  str(e),
            "hint":    ("Install with `pip install mcp` (or "
                         "`pip install 'projmem[mcp]'` if your "
                         "install supports the optional extra)."),
        }
        _sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 2

    server = Server("projmem")

    @server.list_tools()
    async def _list_tools() -> list:
        return [Tool(name=name, description=desc, inputSchema=schema)
                for name, _fn, desc, schema in TOOL_TABLE]

    @server.call_tool()
    async def _call_tool(name: str, arguments: Dict[str, Any]) -> list:
        # Resolve and dispatch.
        fn = next((f for n, f, _d, _s in TOOL_TABLE if n == name), None)
        if fn is None:
            return [TextContent(type="text",
                                 text=json.dumps({"error":
                                                  f"unknown tool {name!r}"}))]
        # Inject default path if not supplied.
        kwargs = dict(arguments or {})
        kwargs.setdefault("path", default_path)
        try:
            result = fn(**kwargs)
        except TypeError as e:
            result = {"error": "bad-arguments", "detail": str(e)}
        except Exception as e:  # tool-level failure surfaced to client
            result = {"error": type(e).__name__, "detail": str(e)}
        return [TextContent(type="text",
                            text=json.dumps(result, default=str))]

    async with stdio_server() as (read, write):
        await server.run(read, write,
                         server.create_initialization_options())
    return 0


def main_sync(default_path: str = ".") -> int:
    """Sync entry point for the CLI subcommand."""
    return asyncio.run(serve(default_path))
