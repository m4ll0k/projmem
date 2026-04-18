"""Symbol lookup helpers."""
from __future__ import annotations
from typing import List

from .store import Store


def find(store: Store, name: str) -> List[dict]:
    return [dict(r) for r in store.symbols_by_name(name)]


_FILE_SUFFIXES = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
                  ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
                  ".hh", ".java", ".rb", ".cs", ".kt", ".swift", ".php",
                  ".scala", ".sh", ".bash",
                  # Round-X bug: package.json / config files were mis-classified
                  # as symbols. Recognize common project-config file types.
                  ".json", ".yaml", ".yml", ".toml", ".lock", ".md")

# Filenames that are ALWAYS files (no slash needed), e.g. bare `package.json`
# typed at the CLI. Was the headline pwnpilot bug.
_FILE_BASENAMES = frozenset({
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "tsconfig.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Makefile", "Dockerfile", "README.md", "LICENSE",
    ".gitignore", ".dockerignore",
})


def _normalize_target_path(s: str) -> str:
    """Delegated to projmem.security.normalize_target_path — kept here
    as a shim so existing callers don't need to update imports."""
    from . import security as _sec
    return _sec.normalize_target_path(s)


def _looks_like_path(s: str) -> bool:
    """Shim — see projmem.security.looks_like_path for canonical
    implementation. Existing callers continue to import from .symbols."""
    from . import security as _sec
    return _sec.looks_like_path(s)


def locate(store: Store, target: str, *,
           force_kind: Optional[str] = None,
           project_root: Optional[str] = None) -> dict:
    """Resolve `target` to one of:
      - {'kind': 'file',   'path': ...}
      - {'kind': 'symbol', 'name': ..., 'defs': [...]}      (may be ambiguous)
      - {'kind': 'symbol', 'name': ..., 'file': ..., 'defs': [...], 'disambiguated': True}

    Accepted shapes:
      - `path/to/file.ext`                file target
      - `path/to/file.ext#symbol`         file#symbol disambiguation
      - `path/to/file.ext#symbol.`        full canonical SCIP-shaped symbol_id (M8)
      - `path/to/file.ext#symbol#`        ditto for type
      - `path/to/file.ext:symbol`         colon form, tolerated
      - `SymbolName`                      symbol lookup across the whole repo
    """
    # M8: full canonical symbol_id (ends in a SCIP-shaped suffix). O(1) lookup.
    from . import symbol_id as _sid
    if _sid.is_symbol_id(target):
        row = store.symbol_by_id(target)
        if row:
            d = dict(row)
            return {"kind": "symbol", "name": d["name"], "file": d["file"],
                    "defs": [d], "disambiguated": True,
                    "file_resolved": True, "symbol_id": target}
        # Symbol_id well-formed but not in store — fail loud, don't fall through.
        parsed = _sid.parse(target)
        fp = parsed.get("file")
        file_indexed = bool(store.conn.execute(
            "SELECT 1 FROM files WHERE path=? LIMIT 1", (fp,)).fetchone()
            ) if fp else False
        return {"kind": "symbol", "name": parsed.get("name", target),
                "file": fp,
                "defs": [], "disambiguated": True,
                "file_resolved": file_indexed, "symbol_id": target,
                "lookup_failed": True}

    # Canonical disambiguation: `file#symbol` (no SCIP suffix)
    if "#" in target:
        fp, sym = target.rsplit("#", 1)
        return _locate_symbol_in_file(store, fp, sym)

    # Also tolerate `file.ext:symbol` — but only when the left side actually
    # looks like a file path (so we don't break `SomeClass:overload`).
    if ":" in target:
        fp, rest = target.split(":", 1)
        if _looks_like_path(fp) and rest and not rest[0].isdigit():
            return _locate_symbol_in_file(store, fp, rest)

    # Round-X fix #1+#2: normalize path-shaped targets BEFORE deciding
    # file vs symbol. `./package.json` → `package.json`. Then test:
    #   - matches an indexed file path? → file
    #   - exists on disk under root? → file (unindexed but real)
    #   - looks like a recognized file shape? → file
    #   - else → symbol
    # `force_kind` flag overrides via `--as-file` / `--as-symbol`.
    normalized = _normalize_target_path(target)

    # Directory target: `.`, `./`, `src/`, or any path that resolves to a
    # directory under the project root. Previously these fell through to
    # symbol resolution and returned an empty pack ("symbol-undefined").
    # Now they're routed to the repo-overview branch in packs.build_pack
    # so `pack .` becomes useful as orientation.
    if force_kind not in ("file", "symbol") and project_root:
        import os as _os
        for cand in (normalized, target):
            if not cand:
                continue
            if cand in (".", "./"):
                return {"kind": "directory", "path": ".",
                        "scope_prefix": "", "is_root": True}
            cand_clean = cand.rstrip("/")
            full = _os.path.join(project_root, cand_clean)
            if _os.path.isdir(full):
                return {"kind": "directory", "path": cand_clean,
                        "scope_prefix": cand_clean + "/",
                        "is_root": False}

    def _try_file(t: str) -> Optional[dict]:
        row = store.conn.execute(
            "SELECT * FROM files WHERE path=?", (t,)).fetchone()
        if row:
            return {"kind": "file", "path": t, "lang": row["lang"],
                    "parser": row["parser"], "stale": bool(row["stale"]),
                    "normalized_from": target if t != target else None}
        if project_root:
            import os as _os
            full = _os.path.join(project_root, t)
            if _os.path.isfile(full):
                return {"kind": "file", "path": t, "resolved": False,
                        "exists_on_disk_but_not_indexed": True,
                        "normalized_from": target if t != target else None}
        return None

    if force_kind == "file":
        # Strict file mode
        hit = _try_file(normalized) or _try_file(target)
        return hit or {"kind": "file", "path": normalized,
                       "resolved": False,
                       "lookup_failed": True}

    if force_kind == "symbol":
        defs = find(store, target)
        return {"kind": "symbol", "name": target, "defs": defs,
                "ambiguous": len(defs) > 1}

    # Auto-detect with file precedence when path-like or proven on disk.
    if _looks_like_path(normalized) or _looks_like_path(target):
        hit = _try_file(normalized) or _try_file(target)
        if hit:
            return hit
        return {"kind": "file", "path": normalized, "resolved": False}

    # Last resort: even bare names get a file probe (handles `Makefile`-like
    # targets that aren't in `_FILE_BASENAMES`).
    hit = _try_file(normalized)
    if hit:
        return hit

    defs = find(store, target)
    return {"kind": "symbol", "name": target, "defs": defs,
            "ambiguous": len(defs) > 1}


def _locate_symbol_in_file(store: Store, file_path: str, name: str) -> dict:
    """Look up `name` scoped to `file_path`. Caller has asked for a specific
    file, so empty result is a hard miss we should surface, not a fallback."""
    defs = [dict(r) for r in store.conn.execute(
        "SELECT * FROM symbols WHERE file=? AND name=?", (file_path, name))]
    file_row = store.conn.execute(
        "SELECT path FROM files WHERE path=?", (file_path,)).fetchone()
    return {
        "kind": "symbol", "name": name, "file": file_path, "defs": defs,
        "disambiguated": True, "file_resolved": file_row is not None,
    }
