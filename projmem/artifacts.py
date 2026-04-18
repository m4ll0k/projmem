"""projmem/artifacts.py — classify files as source vs. artifact.

Motivation: the real-repo benchmark on nodejs/node showed that reverse-lookups
for symbols like ``SafeGetenv`` pulled in 7 CHANGELOG markdown files alongside
the real ``.cc`` call sites. Those CHANGELOG hits are indistinguishable from
source hits to the ref table because they come from the regex fallback
parser — but they don't represent real consumers. Agents reading reverse-dep
output mistake "changelog mentions the function" for "this file calls the
function", which inflates blast-radius estimates.

This module adds a single dimension: *is this file a build output / snapshot
/ baseline / changelog / other mechanical artifact, or is it source?* The
classification is purely path-based, intentionally conservative, and easy
to extend.

Policy:
  - Default queries exclude artifacts from primary ref / reverse-dep counts.
  - Artifacts remain indexed (so the user can find them deliberately) but
    get counted under ``artifact_ref_count`` separately.
  - ``--include-artifacts`` opts back in for callers that need full coverage.

"""
from __future__ import annotations
import os
import re
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Artifact matchers
# ---------------------------------------------------------------------------
#
# Each entry is (regex, reason). Regex is applied against the POSIX-style
# relative path (slashes only, case-sensitive). Reasons are short strings
# that appear in the classification output so a user can see why a path was
# flagged. Keep these precise — false positives erode trust quickly.

_ARTIFACT_DIR_RE = re.compile(
    r"(^|/)("
    r"dist|build|out|generated|__generated__|auto_generated|"
    r"coverage|node_modules|vendor|third_party|third-party|"
    r"tests/baselines|testRunner/fixtures|"
    # TypeScript monorepo: tests/baselines/reference is the big reference
    # output dump (tens of thousands of files) that dominates ref counts.
    r"changelogs|doc/changelogs|"
    # Snapshots, bundles, compiled output — catch common names.
    r"__snapshots__"
    r")(/|$)"
)

# Filename/extension patterns (case-insensitive).
_ARTIFACT_FILENAME_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\.snap$",                re.IGNORECASE), "jest snapshot"),
    (re.compile(r"\.baseline$",            re.IGNORECASE), "baseline output"),
    (re.compile(r"\.bak$",                 re.IGNORECASE), "backup file"),
    (re.compile(r"\.min\.(js|css)$",       re.IGNORECASE), "minified bundle"),
    (re.compile(r"\.bundle\.(js|css)$",    re.IGNORECASE), "bundled output"),
    (re.compile(r"\.map$",                 re.IGNORECASE), "sourcemap"),
    (re.compile(r"\.lock$",                re.IGNORECASE), "lock file"),
    (re.compile(r"\.lockb$",               re.IGNORECASE), "lock file"),
    (re.compile(r"_pb2\.py$",              re.IGNORECASE), "protobuf-generated"),
    (re.compile(r"_pb2_grpc\.py$",         re.IGNORECASE), "grpc-generated"),
    (re.compile(r"\.pb\.go$",              re.IGNORECASE), "protobuf-generated"),
    (re.compile(r"\.d\.ts$",               re.IGNORECASE), "type declaration"),
]

# CHANGELOG markdown files caught separately — they're content, not build
# output, but they still inflate symbol-ref counts from regex-parsed markdown.
_CHANGELOG_RE = re.compile(r"(^|/)CHANGELOG[_.-]?[A-Z0-9]*\.md$",
                            re.IGNORECASE)


def classify_file(path: str) -> Tuple[str, Optional[str]]:
    """Return (classification, reason).

    classification is one of ``"source"``, ``"artifact"``, ``"generated"``.
    reason is a short human-readable string or None.

    "generated" is a subset of "artifact" reserved for mechanically produced
    files (protobuf, type declarations, compiled bundles). Callers that care
    about code-review relevance can treat both as "not source".
    """
    if not path:
        return "source", None

    # Normalize to POSIX separators without resolving (we want the repo-rel
    # view, not a resolved absolute path).
    p = path.replace(os.sep, "/")

    # Directory-level matches first (dist/, node_modules/, tests/baselines/).
    m = _ARTIFACT_DIR_RE.search(p)
    if m:
        return "artifact", f"{m.group(2)}/ directory"

    # Changelog markdown (a common regex-parse false-positive source).
    if _CHANGELOG_RE.search(p):
        return "artifact", "changelog markdown"

    # Filename / extension patterns.
    basename = os.path.basename(p)
    for rx, reason in _ARTIFACT_FILENAME_PATTERNS:
        if rx.search(basename):
            # Distinguish "generated" from plain "artifact" for the patterns
            # that are mechanically derived from source.
            if reason in ("protobuf-generated", "grpc-generated",
                          "type declaration", "minified bundle",
                          "bundled output", "sourcemap"):
                return "generated", reason
            return "artifact", reason

    return "source", None


def is_artifact_path(path: str) -> bool:
    """True when `classify_file(path)` returns anything other than "source"."""
    cls, _ = classify_file(path)
    return cls != "source"


def partition_refs(rows, path_key: str = "file"
                    ) -> Tuple[list, list]:
    """Split a list of ref-like dicts into (source_rows, artifact_rows)
    based on the file path at `path_key`. The input rows are not copied;
    callers that need to preserve immutability should clone first."""
    source_rows, artifact_rows = [], []
    for r in rows:
        p = r.get(path_key) if hasattr(r, "get") else r[path_key]
        if is_artifact_path(p or ""):
            artifact_rows.append(r)
        else:
            source_rows.append(r)
    return source_rows, artifact_rows
