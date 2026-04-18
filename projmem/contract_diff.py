"""Contract diff engine — substrate for the obligation graph.

Takes two labelled sets of contract rows (typically `pre-index` vs live)
and emits:

  added   — contracts present in HEAD, not in BASE
  removed — contracts present in BASE, not in HEAD
  moved   — same (kind, name) but the declaration file(s) changed

Each added/removed contract is annotated with consumer analysis so callers
can ask "flag added, but who reads it?" and "env removed, who still
references it?". Those annotations are what `--as-obligations` projects
into the open_obligations schema.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .store import Store


# Contract kinds we actively diff. Tokens are too noisy to be useful here
# (every string literal is a "token"); guard/dep/script are structural
# metadata, not user-facing contracts.
_DEFAULT_KINDS = ("flag", "env", "schema_field", "event", "entrypoint")


def _bucket(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Group rows by (kind, name). One logical contract may have many rows
    (multiple declaration sites, multiple roles)."""
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("name") is None or r.get("kind") is None:
            continue
        out[(r["kind"], r["name"])].append(r)
    return out


def _flag_name_variants(name: str) -> Set[str]:
    """Return the name variants a flag might show up as downstream.
    argparse/click dests are dash→underscore; commander often exposes
    camelCase; some codebases keep the original `--foo-bar` form in
    error messages. Err on the side of MORE probes — false positives
    here become noise, but false negatives hide orphans."""
    out: Set[str] = {name}
    normalized = name.replace("-", "_")
    out.add(normalized)
    parts = normalized.split("_")
    if len(parts) > 1:
        out.add(parts[0] + "".join(p.capitalize() for p in parts[1:]))
    return out


# Roles that INDICATE a declaration (not a consumption). A contract row
# carrying one of these roles is the flag's "writer side" — it does NOT
# satisfy the obligation by itself. `occurrence` is the raw-token match
# (FLAG_RX picking up `--foo` text anywhere) — in practice this fires on
# the argparse declaration line itself and in docstrings, neither of
# which is a real consumer. Exclude it.
_DECLARATION_ROLES = {"parse", "declare", "write", "write_assign", "occurrence"}


def _consumer_analysis(store: Store, kind: str, name: str,
                       decl_files: Set[str]) -> Dict[str, Any]:
    """For a contract (kind, name) with declaration site(s) `decl_files`,
    return consumer hits. A "consumer" is any reference to the contract
    that is NOT itself a declaration — distinguished by role, not file.
    A small CLI that declares and reads a flag in main.py is satisfied;
    only a flag with zero read/use sites anywhere is an orphan.

    Heuristic. False positives for generic names (e.g. `status`): a ref on
    an unrelated variable named `status` will count. Mitigated by only
    diffing strong kinds by default; token-kind is excluded.
    """
    name_variants = _flag_name_variants(name) if kind == "flag" else {name}
    peers: List[Dict[str, Any]] = []
    for nv in name_variants:
        for r in store.contracts_by_name(nv, kind):
            if r["file"] == "<config>":
                continue
            # Same-file DECLARATION rows don't count as consumers. But a
            # same-file READ role does (typical small CLI pattern).
            if r["file"] in decl_files and r["role"] in _DECLARATION_ROLES:
                continue
            peers.append(dict(r))
    # Refs table: identifier-shaped occurrences. Name refs fall outside
    # the declaration role set by construction (they come from call sites
    # / attribute reads), so we only filter by file to avoid counting the
    # declaration line itself.
    refs: List[Dict[str, Any]] = []
    for cand in name_variants:
        for r in store.refs_by_name(cand):
            if r["file"] in decl_files:
                # Same-file refs CAN be legitimate consumers (e.g. the
                # handler body calls the flag). The ref table already
                # excludes the exact def line, so we keep these.
                refs.append(dict(r))
            else:
                refs.append(dict(r))
    consumer_files = sorted({r["file"] for r in peers}
                            | {r["file"] for r in refs})
    return {
        "consumer_files": consumer_files[:20],
        "consumer_count": len(peers) + len(refs),
        "is_orphan": len(peers) == 0 and len(refs) == 0,
        "probe_names": sorted(name_variants),
    }


def _dangling_analysis(store: Store, kind: str, name: str,
                       removed_files: Set[str]) -> Dict[str, Any]:
    """For a contract that was REMOVED from `removed_files`, find any refs
    or peer contracts that STILL reference the old name elsewhere. Those
    are the dangling sites — the exact "renamed the flag, forgot to update
    the test" bug class."""
    name_variants = _flag_name_variants(name) if kind == "flag" else {name}
    peers: List[Dict[str, Any]] = []
    for nv in name_variants:
        for r in store.contracts_by_name(nv, kind):
            if r["file"] == "<config>":
                continue
            peers.append(dict(r))
    refs: List[Dict[str, Any]] = []
    for cand in name_variants:
        refs.extend(dict(r) for r in store.refs_by_name(cand))
    dangling_sites = [
        {"file": r["file"], "line": r.get("line"), "source": "refs"}
        for r in refs
    ] + [
        {"file": r["file"], "line": r.get("line"), "source": "contracts",
         "role": r.get("role")} for r in peers
    ]
    return {
        "dangling_sites": dangling_sites[:20],
        "dangling_count": len(dangling_sites),
        "is_dangling": len(dangling_sites) > 0,
    }


def compute_diff(store: Store,
                 base_rows: List[Dict[str, Any]],
                 head_rows: List[Dict[str, Any]],
                 kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Compute the contract diff. `base_rows` / `head_rows` come from
    `Store.snapshot_rows(label)` or `Store.live_contract_rows()`.

    `kinds` filters both the diff and the consumer lookups. Defaults to a
    safe list that excludes token (noise) and guard/dep/script (metadata).
    """
    kinds_filter = set(kinds) if kinds else set(_DEFAULT_KINDS)
    base_by_key = _bucket([r for r in base_rows if r.get("kind") in kinds_filter])
    head_by_key = _bucket([r for r in head_rows if r.get("kind") in kinds_filter])
    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    moved: List[Dict[str, Any]] = []
    unchanged_count = 0

    for key in set(base_by_key) | set(head_by_key):
        kind, name = key
        b = base_by_key.get(key, [])
        h = head_by_key.get(key, [])
        if not b and h:
            decl_files = {r["file"] for r in h if r["file"] != "<config>"}
            added.append({
                "kind": kind, "name": name,
                "declarations": [
                    {"file": r["file"], "line": r.get("line"),
                     "role": r.get("role"), "confidence": r.get("confidence"),
                     "context": r.get("context")}
                    for r in h
                ],
                "consumer_analysis": _consumer_analysis(store, kind, name, decl_files),
            })
        elif b and not h:
            removed_files = {r["file"] for r in b if r["file"] != "<config>"}
            removed.append({
                "kind": kind, "name": name,
                "previous_declarations": [
                    {"file": r["file"], "line": r.get("line"),
                     "role": r.get("role"), "confidence": r.get("confidence"),
                     "context": r.get("context")}
                    for r in b
                ],
                "dangling_analysis": _dangling_analysis(store, kind, name, removed_files),
            })
        else:
            b_files = {r["file"] for r in b}
            h_files = {r["file"] for r in h}
            if b_files != h_files:
                # Site-level detail: for each added/removed file, surface
                # the concrete (file, line) coordinates so diff readers
                # can jump to the exact declaration.
                added_sites = [
                    {"file": r["file"], "line": r.get("line"),
                     "role": r.get("role"),
                     "confidence": r.get("confidence")}
                    for r in h if r["file"] in (h_files - b_files)
                ]
                removed_sites = [
                    {"file": r["file"], "line": r.get("line"),
                     "role": r.get("role"),
                     "confidence": r.get("confidence")}
                    for r in b if r["file"] in (b_files - h_files)
                ]
                # Pair removed × added so a reader sees "this is what was
                # there before, this is what's there now" without having
                # to correlate two parallel lists.
                before_after = [
                    {"before": {"file": rs["file"], "line": rs["line"]},
                     "after":  {"file": as_["file"], "line": as_["line"]}}
                    for rs in removed_sites for as_ in added_sites
                ]
                shape = ("1to1" if len(removed_sites) == 1 and len(added_sites) == 1
                         else "split" if len(removed_sites) == 1
                         else "merge" if len(added_sites) == 1
                         else "many-to-many")
                moved.append({
                    "kind": kind, "name": name,
                    "shape":          shape,
                    "added_files":    sorted(h_files - b_files),
                    "removed_files":  sorted(b_files - h_files),
                    "kept_files":     sorted(b_files & h_files),
                    "added_sites":    added_sites,
                    "removed_sites":  removed_sites,
                    "before_after":   before_after,
                })
            else:
                unchanged_count += 1

    added.sort(key=lambda d: (d["kind"], d["name"]))
    removed.sort(key=lambda d: (d["kind"], d["name"]))
    moved.sort(key=lambda d: (d["kind"], d["name"]))
    return {
        "kinds": sorted(kinds_filter),
        "added": added,
        "removed": removed,
        "moved": moved,
        "unchanged_count": unchanged_count,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "moved": len(moved),
            "unchanged": unchanged_count,
        },
        "note": ("Contract-kind filter excludes `token` (noise) and "
                 "structural-metadata kinds (guard/dep/script/entrypoint "
                 "unless explicitly enabled). Consumer analysis probes the "
                 "refs table with name derivations (flag: dashes→underscores, "
                 "camelCase). False positives possible for generic names. "
                 "False negatives for dynamic-dispatch consumers or string-"
                 "keyed lookups."),
    }


def project_obligations(diff: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a contract-diff into the obligation schema.

    One open_obligation row per added-orphan or removed-dangling contract.
    `moved` contracts do NOT produce obligations — moving a declaration is
    not an incomplete change by itself. Callers can inspect `moved` and
    decide per-kind what to do (e.g. a moved flag usually needs a test
    update, but a moved env var may be a refactor with no action needed).
    """
    obligations: List[Dict[str, Any]] = []
    for a in diff.get("added", []):
        ca = a.get("consumer_analysis") or {}
        if not ca.get("is_orphan"):
            continue
        # Each contract kind gets a typed obligation — the consumer knows
        # what the missing side of the contract SHOULD look like.
        kind_ob = {
            "flag": "flag-read",
            "env": "env-read",
            "schema_field": "schema-consumer",
            "event": "event-listener",
            "entrypoint": "entrypoint-consumer",
        }.get(a["kind"], "contract-consumer")
        obligations.append({
            "kind": kind_ob,
            "contract": f"{a['kind']}:{a['name']}",
            "status": "open",
            "declared_at": [
                {"file": d["file"], "line": d["line"], "role": d["role"]}
                for d in a["declarations"]
            ],
            "probe_names": ca.get("probe_names") or [a["name"]],
            "reason": (f"{a['kind']} `{a['name']}` declared at "
                       f"{a['declarations'][0]['file']}:{a['declarations'][0]['line']} "
                       "but no consumer found in repo."),
        })
    for r in diff.get("removed", []):
        da = r.get("dangling_analysis") or {}
        if not da.get("is_dangling"):
            continue
        obligations.append({
            "kind": "dangling-ref",
            "contract": f"{r['kind']}:{r['name']}",
            "status": "open",
            "removed_from": sorted({d["file"] for d in r["previous_declarations"]
                                    if d["file"] != "<config>"}),
            "dangling_sites": da.get("dangling_sites") or [],
            "reason": (f"{r['kind']} `{r['name']}` was removed but "
                       f"{da['dangling_count']} ref(s) still exist elsewhere."),
        })
    return {
        "open_obligations": obligations,
        "coverage_debt": len(obligations),
        "cannot_prove": diff.get("cannot_prove") or [],
        "source": "contract-diff",
        "note": "Obligations derived from contract-diff only. Does NOT "
                "cover temporal/event pairing, runtime-only branches, or "
                "dynamic-dispatch consumers. Those require the temporal "
                "graph + runtime evidence layers.",
    }
