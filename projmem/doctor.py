"""projmem/doctor.py — single-shot health check.

First-run adoption accelerator: one command that surfaces the problems
that actually bite new users (tree-sitter not installed, foreign index,
oversize files silently dropped, regex fallback dominating the parse,
stale files on disk, artifact bleed in the index).

Each finding carries a severity and an actionable suggestion:

    info     — informational, no user action needed
    warning  — degraded behavior; user should consider fixing
    high     — likely wrong results; user should fix before relying on output

Checks are intentionally pure queries against the store + filesystem. No
side effects. Callers run this at any time — typically the first command
in a new checkout.
"""
from __future__ import annotations
import os
from typing import Any, Dict, List, Optional

from . import artifacts as _artifacts
from . import freshness as _fresh


# Severity enum + ordering for sorting the finding list.
INFO = "info"
WARNING = "warning"
HIGH = "high"

_SEVERITY_ORDER = {HIGH: 0, WARNING: 1, INFO: 2}


def _finding(severity: str, code: str, message: str,
             suggestion: Optional[str] = None,
             **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "severity":   severity,
        "code":       code,
        "message":    message,
    }
    if suggestion:
        out["suggestion"] = suggestion
    if extra:
        out["details"] = extra
    return out


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_tree_sitter() -> Dict[str, Any]:
    """tree-sitter installed?"""
    from . import ts_backend
    if ts_backend.available():
        return _finding(INFO, "tree_sitter_installed",
                        "tree-sitter backend is available.")
    return _finding(
        WARNING, "tree_sitter_missing",
        "tree-sitter backend not installed. Falling back to regex parsing.",
        suggestion="pip install -e '.[treesitter]'  (or: pip install "
                   "tree-sitter tree-sitter-language-pack)")


def check_foreign_index(cfg, store) -> Optional[Dict[str, Any]]:
    """Is the store at a different root than the current project?"""
    indexed_root = store.get_meta("root")
    if not indexed_root:
        return _finding(
            WARNING, "index_root_missing",
            "Index DB has no `root` metadata — can't tell what it was "
            "built for.",
            suggestion="Run `projmem index` to rebuild with correct metadata.")
    cur = os.path.realpath(cfg.root)
    idx = os.path.realpath(indexed_root)
    if cur == idx:
        return None
    return _finding(
        HIGH, "foreign_index",
        f"Foreign index: built at {indexed_root!r}, current root is "
        f"{cfg.root!r}. Results may reflect another machine's files.",
        suggestion="Run `projmem index` to rebuild at the current root, or "
                   "export PROJMEM_ALLOW_FOREIGN=1 to acknowledge.",
        indexed_root=indexed_root, current_root=cfg.root)


def check_parser_coverage(store) -> List[Dict[str, Any]]:
    """Regex-fallback dominance on JS/TS, or no parser at all."""
    findings: List[Dict[str, Any]] = []
    rows = list(store.conn.execute(
        "SELECT lang, parser, COUNT(*) AS n FROM files GROUP BY lang, parser"))
    if not rows:
        return findings
    # Per-language rollup
    per_lang: Dict[str, Dict[str, int]] = {}
    for r in rows:
        lang = r["lang"] or "unknown"
        bucket = per_lang.setdefault(lang, {"ast": 0, "regex": 0, "none": 0})
        parser = r["parser"] or "none"
        if parser.startswith("treesitter:") or parser == "ast":
            bucket["ast"] += r["n"]
        elif parser == "regex":
            bucket["regex"] += r["n"]
        else:
            bucket["none"] += r["n"]
    for lang, b in per_lang.items():
        total = b["ast"] + b["regex"] + b["none"]
        if total == 0:
            continue
        ast_pct = 100.0 * b["ast"] / total
        regex_pct = 100.0 * b["regex"] / total
        if lang in ("javascript", "typescript") and regex_pct > 10.0:
            findings.append(_finding(
                WARNING, "regex_fallback_high",
                f"{lang}: {regex_pct:.1f}% of files parsed via regex "
                f"fallback ({b['regex']}/{total}). Same-file ref tracking "
                "may be incomplete on those files.",
                suggestion="Install tree-sitter (see `tree_sitter_missing`) "
                           "and re-run `projmem index --force`.",
                language=lang, ast_files=b["ast"], regex_files=b["regex"]))
        if b["none"] > 0:
            findings.append(_finding(
                WARNING, "no_parser_files",
                f"{lang}: {b['none']} file(s) had no parser at all. "
                "Symbols and refs for those files are missing.",
                language=lang, count=b["none"]))
    return findings


def check_oversize_files(store) -> Optional[Dict[str, Any]]:
    """Has the indexer logged oversize skips recently?"""
    # oversize_skipped is stored only in the last-run stats blob as a JSON
    # meta key when the CLI was invoked. Check the meta table for a
    # persistent hint.
    try:
        row = store.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            ("last_oversize_count",)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        n = int(row["value"])
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return _finding(
        HIGH, "oversize_files_skipped",
        f"{n} file(s) were skipped on the last index because they exceeded "
        "`max_file_bytes`. Symbols and refs in those files are missing.",
        suggestion="Raise `max_file_bytes` in `.projmem/config.json` (e.g. "
                   "`\"max_file_bytes\": 10000000`) and re-run `projmem "
                   "index --force`.",
        count=n)


def check_stale_on_disk(cfg, store,
                        sample_limit: int = 200) -> Optional[Dict[str, Any]]:
    """Sample up to N indexed files, check on-disk hash drift.

    Full scans can be expensive on huge repos (N in thousands). Sampling
    gives a directional signal — if >5% of the sample is stale, recommend
    a full reindex.
    """
    files = list(store.conn.execute(
        "SELECT path FROM files LIMIT ?", (sample_limit,)))
    paths = [r["path"] for r in files]
    if not paths:
        return None
    stale = _fresh.check_paths(store, cfg.root, paths)
    if not stale:
        return None
    sampled = len(paths)
    drift_pct = 100.0 * len(stale) / sampled
    severity = HIGH if drift_pct > 5.0 else WARNING
    return _finding(
        severity, "stale_files_on_disk",
        f"{len(stale)} of the first {sampled} sampled file(s) have "
        f"drifted on disk since the last index ({drift_pct:.1f}%).",
        suggestion="Run `projmem index` or `projmem refresh` to pick up "
                   "the changes.",
        sampled=sampled, stale=len(stale),
        example_paths=[s["path"] for s in stale[:5]])


def check_artifact_bleed(store) -> Optional[Dict[str, Any]]:
    """Warn when a large fraction of the indexed surface looks like artifacts.

    Useful for repos that were indexed without exclude globs and accidentally
    picked up dist/, node_modules/, tests/baselines/, etc."""
    rows = list(store.conn.execute("SELECT path FROM files"))
    if not rows:
        return None
    total = len(rows)
    artifact_count = sum(1 for r in rows
                         if _artifacts.is_artifact_path(r["path"] or ""))
    if artifact_count == 0:
        return None
    pct = 100.0 * artifact_count / total
    if pct < 20.0:
        return _finding(
            INFO, "artifact_files_indexed",
            f"{artifact_count} of {total} indexed files ({pct:.1f}%) are "
            "classified as artifacts (build output / baselines / "
            "changelogs / snapshots).",
            suggestion="This is usually fine. Default ref queries exclude "
                       "them automatically.")
    # Over-threshold: the index is bloated with non-source
    return _finding(
        WARNING, "artifact_bleed_high",
        f"{artifact_count} of {total} indexed files ({pct:.1f}%) are "
        "artifacts. Ref queries will still filter them by default, but the "
        "index is larger than it needs to be.",
        suggestion="Add exclude globs to `.projmem/config.json` "
                   "(e.g. `\"exclude_globs\": [\"dist/**\", "
                   "\"tests/baselines/**\", \"node_modules/**\"]`) and "
                   "re-run `projmem index --force`.",
        total_files=total, artifact_files=artifact_count)


def check_ref_binding(store) -> Optional[Dict[str, Any]]:
    """Low ref-binding percentage means trace / reverse-by-symbol are
    operating on name-level approximations.

    Uses `internal_bound_pct` (bound / refs whose name has a def in the
    index), not the headline `bound_pct`. The headline number is dragged
    down by refs to framework / stdlib / external symbols that can never
    bind regardless of what we do — flagging those as "binding failures"
    confused the user during the audit. The internal percentage is the
    real signal: it isolates refs projmem COULD resolve.
    """
    from . import binding as _binding
    summary = _binding.binding_summary(store)
    if summary["total_refs"] == 0:
        return None
    if summary["internal_bindable"] == 0:
        return None
    pct = summary["internal_bound_pct"]
    if pct >= 80.0:
        return None
    severity = WARNING if pct >= 50.0 else HIGH
    return _finding(
        severity, "low_internal_ref_binding",
        f"Only {pct:.1f}% of internally-bindable refs are bound to a "
        f"specific symbol ({summary['bound_refs']}/{summary['internal_bindable']}). "
        f"({summary['external_refs']} of {summary['total_refs']} total refs "
        "are external/framework symbols and excluded from this measure.) "
        "Trace and reverse-by-symbol operate on approximate name matches "
        "for unbound internal refs.",
        suggestion="This often improves with better module-resolution "
                   "coverage (tree-sitter installed, correct `max_file_bytes`, "
                   "proper include/exclude globs). Re-check `projmem stats` "
                   "→ `parser_coverage`.",
        **summary)


def check_index_empty(store) -> Optional[Dict[str, Any]]:
    """No files indexed at all?"""
    row = store.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()
    if row["n"] == 0:
        return _finding(
            HIGH, "empty_index",
            "The index is empty — no files have been indexed yet.",
            suggestion="Run `projmem index` from the repository root.")
    return None


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def run(cfg, store, *,
        skip_stale_check: bool = False) -> Dict[str, Any]:
    """Execute every check, return the consolidated report.

    `skip_stale_check=True` disables the on-disk hash sampling — useful in
    CI where we don't want to re-hash every indexed file."""
    findings: List[Dict[str, Any]] = []

    # Order matters only for severity rollup; the caller sorts final output.
    findings.append(check_tree_sitter())
    empty = check_index_empty(store)
    if empty:
        findings.append(empty)
    foreign = check_foreign_index(cfg, store)
    if foreign:
        findings.append(foreign)
    findings.extend(check_parser_coverage(store))
    overs = check_oversize_files(store)
    if overs:
        findings.append(overs)
    if not skip_stale_check:
        stale = check_stale_on_disk(cfg, store)
        if stale:
            findings.append(stale)
    bleed = check_artifact_bleed(store)
    if bleed:
        findings.append(bleed)
    binding = check_ref_binding(store)
    if binding:
        findings.append(binding)

    # Sort by severity (high first).
    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9),
                                   f["code"]))
    counts = {
        HIGH:    sum(1 for f in findings if f["severity"] == HIGH),
        WARNING: sum(1 for f in findings if f["severity"] == WARNING),
        INFO:    sum(1 for f in findings if f["severity"] == INFO),
    }
    overall = ("ok" if counts[HIGH] == 0 and counts[WARNING] == 0
               else "attention_needed" if counts[HIGH] == 0
               else "unhealthy")
    # Round-5 P3 + r3 F14: emit BOTH the raw values AND the
    # realpath-resolved equality. When a string-mismatch coexists
    # with realpath equality (the macOS `/tmp` ↔ `/private/tmp`
    # case), surface an explicit `roots_symlink_equivalent` flag and
    # a `roots_note` sentence so the caller doesn't waste time
    # chasing a phantom divergence.
    raw_indexed = store.get_meta("root")
    real_root   = os.path.realpath(cfg.root)
    real_idx    = os.path.realpath(raw_indexed) if raw_indexed else ""
    string_match = bool(raw_indexed and cfg.root == raw_indexed)
    realpath_match = bool(raw_indexed and real_root == real_idx)
    note: Optional[str] = None
    if realpath_match and not string_match:
        note = ("`root` and `indexed_root` differ as strings but resolve "
                "to the same realpath (likely a symlink such as "
                "/tmp → /private/tmp on macOS). Treat as a match.")
    return {
        "schema_version": 1,
        "root":           cfg.root,
        "indexed_root":   raw_indexed,
        "root_realpath":         real_root,
        "indexed_root_realpath": real_idx,
        "roots_match":           realpath_match,
        "roots_string_match":    string_match,
        "roots_symlink_equivalent": realpath_match and not string_match,
        "roots_note":            note,
        "overall":        overall,
        "severity_counts": counts,
        "findings":       findings,
    }
