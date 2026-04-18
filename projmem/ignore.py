"""projmem/ignore.py — `.projmemignore` parser (gitignore-flavored).

Layered on TOP of the artifact heuristics in `artifacts.py` so users
can exclude paths the heuristics don't catch (e.g., a custom build/
output dir, vendored fixture corpora) without disabling the artifact
filter entirely. Shape mimics gitignore where it matters:

  - line-oriented; `#` and blank lines ignored
  - leading `!` negates a previous match (re-includes)
  - leading `/` anchors to repo root
  - trailing `/` matches directories only
  - `**` matches across path segments
  - everything else uses fnmatch semantics, applied to the relative
    POSIX-style path

We deliberately do NOT handle every gitignore corner (no per-directory
.gitignore stacking, no precedence with parent directories) — projmem
is single-root, and the file lives at repo root only. This keeps the
implementation small and matches what users mean when they write a
single ignore file for the index.
"""
from __future__ import annotations
import fnmatch
import os
from typing import List, Optional, Tuple


IGNORE_FILENAME = ".projmemignore"


class IgnoreSpec:
    """Compiled ignore rules. Construct via `load(repo_root)` or `from_text`.

    Use `match(rel_path, is_dir=False)` to test a relative POSIX path.
    Returns True when the path should be IGNORED (skipped by the indexer).
    """

    def __init__(self, rules: List[Tuple[str, bool, bool, bool]]):
        # rules: list of (pattern, negate, dir_only, anchored)
        self.rules = rules

    def __bool__(self) -> bool:
        return bool(self.rules)

    def match(self, rel_path: str, is_dir: bool = False) -> bool:
        """Apply rules in order; later rules override earlier ones.

        Gitignore semantics: a directory rule like `build/` ALSO ignores
        every file beneath that directory, not just the directory entry
        itself. We honor that by checking if any path-segment ancestor
        of `rel_path` is matched by a `dir_only` rule.
        """
        if not self.rules:
            return False
        rel = rel_path.replace(os.sep, "/").lstrip("./")
        ignored = False
        for pattern, negate, dir_only, anchored in self.rules:
            if dir_only:
                # `dir/` ignores `dir`, `dir/x`, `dir/y/z`, etc.
                if _dir_rule_matches(pattern, rel,
                                     anchored=anchored, is_dir=is_dir):
                    ignored = not negate
                continue
            if _pattern_matches(pattern, rel, anchored=anchored,
                                is_dir=is_dir):
                ignored = not negate
        return ignored

    def has_negation_under(self, rel_dir: str) -> bool:
        """True when at least one negation rule targets a path inside
        `rel_dir`. Used by the walker to decide: even though `rel_dir`
        itself is ignored, descend so negated paths beneath it can be
        re-included (matches gitignore semantics for `dir/` + `!dir/keep/`).
        """
        if not self.rules:
            return False
        rel = rel_dir.replace(os.sep, "/").rstrip("/")
        for pattern, negate, dir_only, anchored in self.rules:
            if not negate:
                continue
            # Strip wildcard tail so we can compare prefixes.
            prefix = pattern.split("*", 1)[0].rstrip("/")
            if not prefix:
                # Bare `!*.py` style — could match anywhere; descend.
                return True
            if (prefix == rel
                    or prefix.startswith(rel + "/")
                    or rel.startswith(prefix + "/")):
                return True
        return False

    def to_exclude_globs(self) -> List[str]:
        """Best-effort conversion to fnmatch globs for paths that don't
        need negation. Used to feed the existing discovery.walk()
        exclude_globs path. Negations are silently dropped (the caller
        consults `match()` directly when negation matters)."""
        out: List[str] = []
        for pattern, negate, dir_only, anchored in self.rules:
            if negate:
                continue
            p = pattern
            if dir_only:
                p = p + "/**"
            out.append(p)
        return out


def load(repo_root: str) -> IgnoreSpec:
    """Read `<repo_root>/.projmemignore` if it exists. Empty/missing →
    empty spec that matches nothing."""
    path = os.path.join(repo_root, IGNORE_FILENAME)
    if not os.path.isfile(path):
        return IgnoreSpec(rules=[])
    try:
        with open(path, "r", encoding="utf-8") as f:
            return from_text(f.read())
    except OSError:
        return IgnoreSpec(rules=[])


def from_text(text: str) -> IgnoreSpec:
    """Parse rule text. Used by `load` and tests."""
    rules: List[Tuple[str, bool, bool, bool]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        # Allow `\#` escape so a literal hash can start a pattern.
        if line.startswith("\\#"):
            line = line[1:]
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        if not line:
            continue
        rules.append((line, negate, dir_only, anchored))
    return IgnoreSpec(rules=rules)


def _dir_rule_matches(pattern: str, rel: str, *, anchored: bool,
                      is_dir: bool) -> bool:
    """A `dir/` rule matches the directory entry AND every descendant.
    e.g. `build/` matches `build` (dir), `build/x.py` (file under it),
    and `build/keep/important.py`. Anchored rules only match the
    repo-root form."""
    # Direct hit on the dir entry itself.
    if is_dir and _pattern_matches(pattern, rel, anchored=anchored,
                                    is_dir=is_dir):
        return True
    # Descendant: rel starts with `<pattern>/` after any non-anchored
    # prefix. We test both anchored (root-relative) and basename
    # variants so `build/` matches `build/x.py` AND `pkg/build/x.py`.
    norm = pattern.rstrip("/")
    if rel == norm:
        return True
    if rel.startswith(norm + "/"):
        return True
    if not anchored:
        # Allow match anywhere: `<anything>/<pattern>/<anything>`
        if "/" + norm + "/" in "/" + rel + "/":
            return True
    return False


def _pattern_matches(pattern: str, rel: str, *, anchored: bool,
                     is_dir: bool) -> bool:
    """fnmatch with two extensions:
       - `**` becomes a multi-segment wildcard (effectively `*` for
         fnmatch but we test both anchored and basename forms)
       - non-anchored patterns also match any path SUFFIX (so
         `node_modules` matches `src/foo/node_modules/x.js`)
    """
    # Direct fnmatch.
    if fnmatch.fnmatch(rel, pattern):
        return True
    # Replace `**` for fnmatch (it doesn't understand it natively).
    norm = pattern.replace("**/", "").replace("/**", "/*")
    if fnmatch.fnmatch(rel, norm):
        return True
    if anchored:
        return False
    # Basename match (`*.log` → match any basename).
    base = os.path.basename(rel)
    if "/" not in pattern and fnmatch.fnmatch(base, pattern):
        return True
    # Path-segment match: `vendor` should hit `a/b/vendor` and
    # `a/b/vendor/x`. We check if any path segment equals the pattern,
    # or if the rel path starts with `<pattern>/` after any prefix.
    if "/" not in pattern:
        parts = rel.split("/")
        if pattern in parts:
            return True
    else:
        # Multi-segment, non-anchored — match if rel CONTAINS the pattern
        # at a segment boundary (`docs/build` matches `pkg/docs/build/x`).
        prefix = pattern.split("*", 1)[0].rstrip("/")
        if prefix and (("/" + prefix + "/") in ("/" + rel + "/")):
            return True
    return False
