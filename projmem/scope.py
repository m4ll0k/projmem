"""Scope-boundary labeling. Every command result row should carry an
`in_scope` flag so AI agents can filter vendor noise automatically.

Round-X feedback: `contract-drift` produced 98% third-party package.json
noise when `chrome/` was indexed; `missing-paths` surfaced 148 vendor
fixture paths. Both are cases where the answer "did the tool find a
problem?" depends on knowing whether each row is in YOUR code or in
VENDORED code that you don't own.

Vendor prefix detection is heuristic, layered:
  1. Built-in prefix list (`_DEFAULT_VENDOR_PREFIXES`) — covers the obvious
     common cases (`chrome/`, `node_modules/`, `vendor/`, `third_party/`,
     `_archive/`, `bower_components/`).
  2. The last-index session's `--exclude` globs — anything explicitly
     excluded but visible in the index counts as vendor (the user said
     so by excluding it).
  3. User config: `.projmem/config.json::vendor_prefixes` (future).
"""
from __future__ import annotations
import json
from typing import Iterable, List, Optional

_DEFAULT_VENDOR_PREFIXES: List[str] = [
    "chrome/", "node_modules/", "vendor/", "third_party/",
    "_archive/", "bower_components/", "external/", "deps/",
    "Pods/",  # iOS
    "build/", "dist/", "out/", "target/",  # build outputs
    ".tox/", ".venv/", "venv/", "env/",
]


def get_vendor_prefixes(store) -> List[str]:
    """Build the effective vendor-prefix list for this store. Combines
    built-ins with last-index `--exclude` globs that look like prefixes.
    """
    prefixes = list(_DEFAULT_VENDOR_PREFIXES)
    raw = store.get_meta("last_index_session")
    if raw:
        try:
            sess = json.loads(raw)
        except json.JSONDecodeError:
            sess = {}
        for g in sess.get("exclude_globs_effective", []) or []:
            # Convert glob to a prefix when it has a clear static prefix
            # (e.g. `chrome/**` → `chrome/`). Skip pure-pattern globs.
            stem = g.split("*", 1)[0].rstrip("/")
            if stem and "/" not in stem.lstrip("./"):
                prefixes.append(stem + "/")
            elif stem:
                prefixes.append(stem + "/")
    # De-dup, normalize
    seen = set()
    out: List[str] = []
    for p in prefixes:
        p = p.replace("\\", "/")
        if not p.endswith("/"):
            p += "/"
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def is_in_scope(path: Optional[str], vendor_prefixes: Iterable[str]) -> bool:
    """True if `path` is NOT under any vendor prefix. Empty / placeholder
    paths (e.g. `<config>`) are treated as in_scope — they're
    projmem-internal markers, not vendor code."""
    if not path or path == "<config>":
        return True
    p = path.replace("\\", "/")
    return not any(p == vp.rstrip("/") or p.startswith(vp)
                   for vp in vendor_prefixes)
