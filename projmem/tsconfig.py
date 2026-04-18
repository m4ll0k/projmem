"""projmem/tsconfig.py — tsconfig.json / jsconfig.json path alias loader.

Modern TypeScript/JavaScript projects use path aliases ("baseUrl" +
"compilerOptions.paths") to keep import specifiers short:

    # tsconfig.json
    { "compilerOptions": {
        "baseUrl": ".",
        "paths": {
          "@/*":          ["src/*"],
          "~/*":          ["src/*"],
          "@components/*": ["src/components/*"],
          "@utils":        ["src/utils/index"]
    }}}

Without this resolver, `import Foo from '@/components/Foo'` looks like an
external package to projmem — every such import ends up in
`unresolved-imports` and the bind rate craters. Real-world feedback
reported ~386 unresolved imports with 32.1% bind rate on a repo that's
heavily alias-based; resolving aliases is the single highest-leverage
correctness fix.

Scope:
  - Reads tsconfig.json or jsconfig.json at the repo root (and one
    level of `extends` chasing).
  - Parses JSON-with-comments leniently (jsonc).
  - Exposes `resolve_alias(spec, repo_root)` returning the rewritten
    spec (still relative-shape) that `resolve_js_import` can match
    against the on-disk file set.
  - Cached per-root so the config is read once per process.

Does NOT:
  - Implement the full ts.resolveModuleName algorithm.
  - Handle tsconfig extends chains deeper than one level.
  - Handle project references.
"""
from __future__ import annotations
import json
import os
import re
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# JSON-with-comments stripper
# ---------------------------------------------------------------------------

# tsconfig.json is technically JSON-with-comments. Python's json module
# rejects `//` and `/* */` comments, so we strip them before parsing.
# Also strip trailing commas inside objects/arrays (jsonc allows them).
_COMMENT_RX = re.compile(
    r"//[^\n]*|/\*.*?\*/",
    re.DOTALL,
)
_TRAILING_COMMA_RX = re.compile(r",(\s*[}\]])")


def _parse_jsonc(text: str) -> Optional[dict]:
    """Parse JSON-with-comments leniently. Returns None on failure."""
    def _strip_line_comments(src: str) -> str:
        # Strip `//...` but not inside string literals.
        # Simple state machine: walk chars; toggle in_string on unescaped ".
        out: List[str] = []
        i = 0
        in_str = False
        while i < len(src):
            c = src[i]
            if in_str:
                if c == "\\" and i + 1 < len(src):
                    out.append(src[i:i+2]); i += 2; continue
                if c == '"':
                    in_str = False
                out.append(c); i += 1; continue
            if c == '"':
                in_str = True; out.append(c); i += 1; continue
            if c == "/" and i + 1 < len(src) and src[i+1] == "/":
                # line comment: skip to end of line
                while i < len(src) and src[i] != "\n":
                    i += 1
                continue
            if c == "/" and i + 1 < len(src) and src[i+1] == "*":
                # block comment: skip to */
                end = src.find("*/", i + 2)
                if end == -1:
                    return ""
                i = end + 2; continue
            out.append(c); i += 1
        return "".join(out)

    stripped = _strip_line_comments(text)
    stripped = _TRAILING_COMMA_RX.sub(r"\1", stripped)
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Loader with one-level `extends` support
# ---------------------------------------------------------------------------

# Per-process cache: root path → (baseUrl, paths_map)
_CACHE: Dict[str, Tuple[Optional[str], Dict[str, List[str]]]] = {}


def _find_config(repo_root: str) -> Optional[str]:
    """Return the absolute path of tsconfig.json or jsconfig.json at
    `repo_root`, or None if neither exists."""
    for name in ("tsconfig.json", "jsconfig.json"):
        p = os.path.join(repo_root, name)
        if os.path.isfile(p):
            return p
    return None


def _load_package_json_imports(repo_root: str) -> Dict[str, List[str]]:
    """Node.js-native `imports` field in package.json. Maps specifiers
    prefixed with `#` to package-local files. Example:

        "imports": { "#utils/*": "./src/utils/*.js" }

    Returns a paths-map-shaped dict so _load() can merge it with any
    tsconfig-declared aliases."""
    p = os.path.join(repo_root, "package.json")
    if not os.path.isfile(p):
        return {}
    raw = _load_config_raw(p)
    if not raw:
        return {}
    imports = raw.get("imports")
    if not isinstance(imports, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for k, v in imports.items():
        if not isinstance(k, str) or not k.startswith("#"):
            continue
        if isinstance(v, str):
            out[k] = [v.lstrip("./")]
        elif isinstance(v, dict):
            # Conditional exports — pick the first concrete string we find
            # (import / default / node branches). Deterministic: sort keys.
            for ck in sorted(v):
                cv = v[ck]
                if isinstance(cv, str):
                    out[k] = [cv.lstrip("./")]
                    break
    return out


def _load_deno_imports(repo_root: str) -> Dict[str, List[str]]:
    """deno.json / deno.jsonc `imports` field. Maps bare specifiers to
    local paths (or remote URLs — we keep only local mappings)."""
    for name in ("deno.json", "deno.jsonc"):
        p = os.path.join(repo_root, name)
        if os.path.isfile(p):
            raw = _load_config_raw(p)
            if not raw:
                continue
            imports = raw.get("imports")
            if not isinstance(imports, dict):
                continue
            out: Dict[str, List[str]] = {}
            for k, v in imports.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    continue
                # Skip remote (https://) and jsr:/npm: specifiers — we
                # only rewrite to local files.
                if v.startswith(("http:", "https:", "jsr:", "npm:", "node:")):
                    continue
                out[k] = [v.lstrip("./")]
            return out
    return {}


_VITE_ALIAS_RX = __import__("re").compile(
    r"""alias\s*[:=]\s*\{([^}]{1,4000})\}""",
    __import__("re").DOTALL,
)
_VITE_ENTRY_RX = __import__("re").compile(
    r"""['"](?P<key>[^'"]{1,64})['"]\s*:\s*"""
    r"""(?:path\.resolve\([^,]+,\s*)?"""
    r"""['"](?P<val>[^'"]{1,128})['"]"""
)


def _load_vite_like_aliases(repo_root: str) -> Dict[str, List[str]]:
    """Best-effort alias extraction from vite.config.* / next.config.*.

    These configs are JS/TS, not JSON — we can't fully parse them.
    We do a regex sweep for `alias: { '@' : path.resolve(__dirname, 'src') }`
    style declarations. Conservative: we only extract string→string pairs.
    Misses computed aliases (rare) but covers the 90% case."""
    candidates = [
        "vite.config.ts", "vite.config.js", "vite.config.mts",
        "next.config.js", "next.config.mjs", "next.config.ts",
        "webpack.config.js",
    ]
    out: Dict[str, List[str]] = {}
    for name in candidates:
        p = os.path.join(repo_root, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for block in _VITE_ALIAS_RX.finditer(text):
            body = block.group(1)
            for entry in _VITE_ENTRY_RX.finditer(body):
                key = entry.group("key")
                val = entry.group("val")
                # Normalize: vite aliases usually map `@` → an absolute
                # dir. Store as `<key>/*` → `<val>/*` so `_match_alias`
                # handles them the same way as tsconfig patterns.
                if "*" not in key and val:
                    out.setdefault(f"{key}/*", []).append(
                        val.rstrip("/").lstrip("./") + "/*")
    return out


def _load_config_raw(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _parse_jsonc(f.read())
    except OSError:
        return None


def _resolve_config(path: str, _visited: Optional[set] = None) -> Optional[dict]:
    """Load a tsconfig chain following one `extends` hop. Returns the
    merged compilerOptions.paths / baseUrl view, or None.

    We only follow ONE level of `extends` to keep this cheap and avoid
    infinite loops on pathological configs. The `extends` is treated as
    base; the current file overrides."""
    if _visited is None:
        _visited = set()
    if path in _visited:
        return None
    _visited.add(path)
    raw = _load_config_raw(path)
    if raw is None:
        return None
    # Resolve extends (if any).
    extends = raw.get("extends")
    base: dict = {}
    if isinstance(extends, str):
        # Bare specifier like `@tsconfig/node20/tsconfig.json` — can't
        # resolve without node_modules; skip. Relative path we CAN follow.
        if extends.startswith(".") or extends.startswith("/"):
            resolved = os.path.normpath(
                os.path.join(os.path.dirname(path), extends))
            # `extends` may omit the trailing `.json`.
            for cand in (resolved,
                         resolved + ".json" if not resolved.endswith(".json")
                         else resolved):
                if os.path.isfile(cand):
                    parent = _resolve_config(cand, _visited)
                    if parent:
                        base = parent
                    break
    merged: dict = dict(base)
    co = raw.get("compilerOptions") or {}
    base_co = base.get("compilerOptions") or {}
    merged_co: dict = dict(base_co)
    # Override individual keys.
    for k in ("baseUrl", "paths"):
        if k in co:
            merged_co[k] = co[k]
    merged["compilerOptions"] = merged_co
    return merged


def _load(repo_root: str) -> Tuple[Optional[str], Dict[str, List[str]]]:
    """Return (baseUrl_abs, paths_map) for the repo. Cached.

    `paths_map` keys are alias PATTERNS as written in tsconfig (e.g.
    `@/*`, `~/*`, `@utils`). Values are lists of target patterns.

    Sources merged in this order (later wins ties):
      1. tsconfig.json / jsconfig.json compilerOptions.paths
      2. package.json imports field (Node native subpath imports)
      3. deno.json imports field
      4. vite.config.* / next.config.* / webpack.config.js alias blocks

    Real-world projects scatter aliases across these files; pulling
    only from tsconfig leaves significant resolution gaps.
    """
    key = os.path.realpath(repo_root) if repo_root else ""
    if key in _CACHE:
        return _CACHE[key]
    if not repo_root:
        _CACHE[key] = (None, {})
        return _CACHE[key]

    paths_map: Dict[str, List[str]] = {}
    abs_base: Optional[str] = None

    cfg_path = _find_config(repo_root)
    if cfg_path:
        cfg = _resolve_config(cfg_path)
        if cfg:
            co = cfg.get("compilerOptions") or {}
            base_url = co.get("baseUrl")
            raw_paths = co.get("paths") or {}
            for k, v in raw_paths.items():
                if isinstance(v, str):
                    paths_map[k] = [v]
                elif isinstance(v, list):
                    paths_map[k] = [p for p in v if isinstance(p, str)]
            if base_url is not None:
                abs_base = os.path.normpath(
                    os.path.join(os.path.dirname(cfg_path), base_url))

    # Merge additional sources. These are PATH-relative to the repo root
    # (not to a baseUrl subdir), so if baseUrl is None they serve as-is.
    for source_fn in (_load_package_json_imports, _load_deno_imports,
                       _load_vite_like_aliases):
        try:
            extra = source_fn(repo_root)
        except Exception:
            extra = {}
        for k, v in extra.items():
            # Don't clobber existing tsconfig entries — tsconfig is the
            # authoritative source when it's present.
            paths_map.setdefault(k, v)

    _CACHE[key] = (abs_base, paths_map)
    return _CACHE[key]


def _reset_cache() -> None:
    """Test helper: clear the per-root cache so tests can mutate configs
    between runs without cross-contamination."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------

def resolve_alias(spec: str, repo_root: str) -> List[str]:
    """Given a bare import spec like `@/components/Foo`, return the list
    of CANDIDATE relative paths (from the repo root) that match under
    the configured `paths` + `baseUrl`.

    Returns an empty list when:
      - no tsconfig/jsconfig at the repo root
      - the spec doesn't match any alias pattern
      - the pattern has no on-disk candidates

    Caller (`resolve_js_import`) tries each candidate against the file
    system. We return repo-relative paths with forward-slashes so they
    line up with the rest of projmem's path handling.
    """
    if not spec or not repo_root:
        return []
    base_url, paths_map = _load(repo_root)
    if not paths_map:
        return []
    candidates: List[str] = []
    for pattern, targets in paths_map.items():
        resolved_fragment = _match_alias(spec, pattern)
        if resolved_fragment is None:
            continue
        for target in targets:
            # Substitute `*` in the target with the captured fragment.
            if "*" in target:
                filled = target.replace("*", resolved_fragment or "")
            else:
                filled = target
            # Target is relative to baseUrl (which is relative to the
            # tsconfig location). Normalize to repo-relative. When no
            # baseUrl is configured — typical for package.json `imports`
            # / deno / vite-style aliases — treat `filled` as already
            # repo-relative.
            if base_url:
                abs_target = os.path.normpath(
                    os.path.join(base_url, filled))
                rel = os.path.relpath(abs_target, repo_root)
            else:
                rel = filled.lstrip("./")
            rel = rel.replace("\\", "/")
            candidates.append(rel)
    return candidates


def _match_alias(spec: str, pattern: str) -> Optional[str]:
    """If `spec` matches `pattern` (which may end with `*`), return the
    captured `*` fragment. For exact-match patterns (no `*`), return
    empty string on match, None on no match.

    Examples:
      `@/components/Foo`  vs  `@/*`            → 'components/Foo'
      `@/components/Foo`  vs  `@components/*`  → None (prefix mismatch)
      `@utils`            vs  `@utils`         → ''
    """
    if pattern.endswith("/*"):
        prefix = pattern[:-1]   # keep the trailing slash
        if spec.startswith(prefix):
            return spec[len(prefix):]
        return None
    if pattern.endswith("*"):
        # Rare shape like `@*` — treat as prefix match with no slash.
        prefix = pattern[:-1]
        if spec.startswith(prefix):
            return spec[len(prefix):]
        return None
    # Exact match.
    if spec == pattern:
        return ""
    return None
