"""File walking. Skips common junk dirs; honors include/exclude globs at BOTH
directory and file level (directory-level pruning is a perf + correctness gain
on trees like node_modules/ and vendor/)."""
from __future__ import annotations
import fnmatch
import os
from typing import Iterator, List, Tuple

from .utils import SKIP_DIRS, SKIP_DIR_SUFFIXES, lang_of, rel


# --- Oversize-file tracking -------------------------------------------------
# Files that exceed `max_file_bytes` are skipped during walk. Previously this
# skip was silent, which hid critical files from the index. We record each
# oversize skip here so the indexer can surface a HIGH-severity warning with
# path + size, letting the caller either raise the limit or narrow the scope
# deliberately.
_OVERSIZE_SKIPS: List[dict] = []


def _reset_oversize() -> None:
    _OVERSIZE_SKIPS.clear()


def _record_oversize(rel_path: str, size: int, limit: int) -> None:
    _OVERSIZE_SKIPS.append({"path": rel_path, "size": size, "limit": limit})


def consume_oversize_skips() -> List[dict]:
    """Return and clear the list of files skipped for exceeding
    `max_file_bytes` during the most recent walk."""
    out = list(_OVERSIZE_SKIPS)
    _OVERSIZE_SKIPS.clear()
    return out


def _match_any(path: str, patterns: List[str]) -> bool:
    """fnmatch any pattern against `path` or its basename. `**` is treated as `*`
    for our purposes (fnmatch is shallow; this is the pragmatic behavior)."""
    if not patterns:
        return False
    base = os.path.basename(path)
    for p in patterns:
        p_norm = p.replace("**/", "").replace("/**", "")
        if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p_norm):
            return True
        if "/" not in p_norm and fnmatch.fnmatch(base, p_norm):
            return True
    return False


def _is_excluded_prefix(rel_dir: str, exclude_globs: List[str]) -> bool:
    """True if rel_dir matches the static prefix of any exclude pattern.
    Helper for the exclude_wins path to prune `chrome/` when an exclude
    is `chrome/**`."""
    for p in exclude_globs:
        prefix = p.split("*", 1)[0].rstrip("/")
        if prefix and (rel_dir == prefix or rel_dir.startswith(prefix + "/")):
            return True
    return False


def _dir_pruned(rel_dir: str, exclude_globs: List[str],
                include_globs: List[str] | None = None) -> bool:
    """A directory is pruned if an exclude matches AND no include rescues it.

    Precedence rule (decision: include beats exclude when both match):
      - If any include pattern has a prefix that still lives beneath rel_dir
        (e.g. `chrome/src/**` under an `chrome/**` exclude), we do NOT prune —
        we descend so the include can match files below.
      - If any include pattern matches rel_dir itself, we also do not prune.
    This fixes the round-2 report: `--exclude 'chrome/**'` combined with
    `--include 'chrome/src/**'` was producing zero files instead of the
    included subtree, because the exclude blanket-pruned the whole dir.
    """
    include_globs = include_globs or []

    def _include_reaches_into(dir_path: str) -> bool:
        if not include_globs:
            return False
        for ip in include_globs:
            ip_prefix = ip.split("*", 1)[0].rstrip("/")
            if not ip_prefix:
                return True  # e.g. `**/*.py` — matches under every dir
            # include's prefix equals dir, or is below dir, or dir is below prefix
            if (ip_prefix == dir_path
                    or ip_prefix.startswith(dir_path + "/")
                    or dir_path.startswith(ip_prefix + "/")
                    or _match_any(dir_path, [ip])):
                return True
        return False

    excluded = False
    if _match_any(rel_dir, exclude_globs):
        excluded = True
    else:
        for p in exclude_globs:
            prefix = p.split("*", 1)[0].rstrip("/")
            if prefix and (rel_dir == prefix or rel_dir.startswith(prefix + "/")):
                excluded = True
                break
    if not excluded:
        return False
    # excluded — but does an include rescue it?
    return not _include_reaches_into(rel_dir)


def walk(root: str, include_globs: List[str] | None = None,
         exclude_globs: List[str] | None = None,
         max_bytes: int = 1_000_000,
         exclude_wins: bool = False,
         ignore_spec: object | None = None) -> Iterator[str]:
    """Walk the project. By default, `--include` wins over `--exclude` so
    narrow includes can rescue files inside excluded subtrees (round-3 fix).

    `exclude_wins=True` (round-X feedback): EXCLUDE always subtracts, even
    inside an included subtree. Enables `--include 'deploy/**' --exclude
    'deploy/patches/**'` to keep wrappers but drop vendor patches.

    `ignore_spec` (when given) is a `projmem.ignore.IgnoreSpec` consulted
    for `.projmemignore` rules. Applied as a hard filter ON TOP of the
    above logic so negation patterns (`!keep.txt`) work as a user would
    expect, regardless of include/exclude semantics.
    """
    include_globs = include_globs or []
    exclude_globs = exclude_globs or []
    # Reset the oversize-file record for each fresh walk so repeated
    # index runs don't inherit stale warnings.
    _reset_oversize()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = rel(dirpath, root) if dirpath != root else ""
        kept = []
        for d in dirnames:
            child_rel = os.path.join(rel_dir, d) if rel_dir else d
            # Built-in skip dirs (node_modules, .git, build, dist, ...).
            # `.projmemignore` can RESCUE these via a negation rule —
            # so `!build/keep/` works even when `build` is on the
            # default SKIP_DIRS list.
            ignore_negate_rescue = (
                ignore_spec is not None
                and hasattr(ignore_spec, "has_negation_under")
                and ignore_spec.has_negation_under(child_rel))
            if d in SKIP_DIRS and not ignore_negate_rescue:
                continue
            if d.startswith(".") and not ignore_negate_rescue:
                continue
            if any(d.endswith(suf) for suf in SKIP_DIR_SUFFIXES) \
                    and not ignore_negate_rescue:
                continue
            # `.projmemignore`: prune the whole subtree when matched at
            # directory level — UNLESS a later negation rule re-includes
            # something beneath it. `build/` + `!build/keep/` must
            # descend into `build/` so `keep/` is reachable; the
            # file-level check below subtracts unwanted siblings.
            if ignore_spec is not None and getattr(ignore_spec, "match",
                                                    None):
                if ignore_spec.match(child_rel, is_dir=True):
                    if not ignore_negate_rescue:
                        continue
            # In exclude_wins mode the include rescue is disabled at dir level.
            if exclude_wins:
                # Plain exclude check: pruned if any exclude matches and no
                # rescue path (since include cannot override).
                if _match_any(child_rel, exclude_globs) or _is_excluded_prefix(
                        child_rel, exclude_globs):
                    continue
            else:
                if _dir_pruned(child_rel, exclude_globs, include_globs):
                    continue
            kept.append(d)
        dirnames[:] = kept

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            r = rel(full, root)
            # `.projmemignore` is checked first so it can both subtract
            # (positive rule) and rescue (negation `!path`).
            if ignore_spec is not None and getattr(ignore_spec, "match",
                                                    None):
                if ignore_spec.match(r, is_dir=False):
                    continue
            # File-level: in exclude_wins mode, exclude always trumps include.
            if exclude_wins:
                if _match_any(r, exclude_globs):
                    continue
                if include_globs and not _match_any(r, include_globs):
                    continue
            elif include_globs:
                # include-wins (default round-3 behavior)
                if not _match_any(r, include_globs):
                    continue
            elif _match_any(r, exclude_globs):
                continue
            try:
                sz = os.path.getsize(full)
                if sz > max_bytes:
                    # P0 fix — previously this was a silent skip, which
                    # hid structurally critical files from the index
                    # (TypeScript's src/compiler/checker.ts at 3.15 MB
                    # was dropped by the 3 MB default, erasing 50k+
                    # symbols). Record into a thread-local list so the
                    # indexer can emit a HIGH-severity warning.
                    _record_oversize(r, sz, max_bytes)
                    continue
            except OSError:
                continue
            if lang_of(full) == "other":
                if not _looks_texty(full):
                    continue
            yield full


def walk_with_excluded(root: str, include_globs: List[str] | None = None,
                       exclude_globs: List[str] | None = None,
                       max_bytes: int = 1_000_000,
                       exclude_wins: bool = False,
                       ignore_spec: object | None = None
                       ) -> Tuple[List[str], List[str]]:
    """Like `walk`, but also returns a list of top-level directories that were
    pruned — so callers can say "these exist but were excluded"."""
    included = list(walk(root, include_globs, exclude_globs, max_bytes,
                         exclude_wins=exclude_wins,
                         ignore_spec=ignore_spec))
    excluded_top: List[str] = []
    try:
        for entry in sorted(os.listdir(root)):
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            if entry in SKIP_DIRS or entry.startswith("."):
                excluded_top.append(entry)
                continue
            if _dir_pruned(entry, exclude_globs or []):
                excluded_top.append(entry)
                continue
            if ignore_spec is not None and getattr(ignore_spec, "match", None):
                if ignore_spec.match(entry, is_dir=True):
                    excluded_top.append(entry)
    except OSError:
        pass
    return included, excluded_top


def _looks_texty(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        if b"\x00" in chunk:
            return False
        return True
    except OSError:
        return False
