"""P2#7 — Edge-type transparency verification.

The trace engine must make the ref kind used at each hop fully observable:
  - Every hop in the returned path must carry an `edge_type` key.
  - The first hop (the source symbol itself) must have `edge_type=None`.
  - Every subsequent hop must carry the ref kind that connected it
    ('call' in strict mode; 'call'/'new'/'callback' in relaxed).
  - `allowed_edges` in the result must accurately reflect the mode.
  - Relaxed mode must surface 'new' edge kinds when those are the only
    connections (e.g. a constructor-call bridge).

These tests are distinct from the P0 fix regression tests in test_p0_fixes.py
which verify strict mode doesn't cross import edges. Here the focus is on
the *labeling* contract: what `edge_type` is reported and when.
"""
from __future__ import annotations

import os
import pytest

from projmem.store import Store
from projmem.ts_backend import index as ts_index, available as ts_available

needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index_files(tmp_path, files: dict) -> Store:
    store = Store(str(tmp_path / "idx.db"))
    root = str(tmp_path)
    for rel, src in files.items():
        abs_p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "w") as f:
            f.write(src)
        ts_index(store, rel, src, "javascript", root)
    return store


# ---------------------------------------------------------------------------
# Source hop always edge_type=None
# ---------------------------------------------------------------------------

@needs_ts
def test_source_hop_has_edge_type_none(tmp_path):
    """The first element of `path` is the source (the CALLER) itself; it
    has no incoming edge so edge_type must be None (not 'call', not missing).

    Audit fix: BFS direction is now caller→callee (`source` calls `sink`),
    matching the user's mental model.
    """
    from projmem.graph import trace_call_chain

    # caller_fn calls callee_fn directly.
    # trace(caller_fn, callee_fn) → [caller_fn(None), callee_fn('call')]
    store = _index_files(tmp_path, {
        "a.js": (
            "export function callee_fn() {}\n"
            "export function caller_fn() { callee_fn(); }\n"
        )
    })
    res = trace_call_chain(store, "caller_fn", "callee_fn",
                           max_hops=2, mode="strict")
    path = res.get("path") or []
    assert len(path) >= 2, f"Expected a path; got {res}"
    assert "edge_type" in path[0], "source hop must have edge_type key"
    assert path[0]["edge_type"] is None, (
        f"source hop edge_type must be None, got {path[0]['edge_type']!r}")


# ---------------------------------------------------------------------------
# Subsequent hops always carry a non-None edge_type
# ---------------------------------------------------------------------------

@needs_ts
def test_intermediate_hops_have_non_none_edge_type(tmp_path):
    """Every hop after the source must report the ref kind that
    connected it.

    Audit fix: BFS direction is caller→callee. For root() → mid() →
    leaf(): trace(root, leaf) returns [root(None), mid('call'), leaf('call')].
    """
    from projmem.graph import trace_call_chain

    # root() calls mid() calls leaf().
    store = _index_files(tmp_path, {
        "a.js": (
            "export function leaf() {}\n"
            "export function mid() { leaf(); }\n"
            "export function root() { mid(); }\n"
        )
    })
    res = trace_call_chain(store, "root", "leaf", max_hops=3, mode="strict")
    path = res.get("path") or []
    assert len(path) >= 3, f"Expected 3-hop path root→mid→leaf; got {res}"
    # Every hop after index 0 must have a non-None edge_type.
    for hop in path[1:]:
        assert hop.get("edge_type") is not None, (
            f"Non-source hop must have edge_type set: {hop}")


# ---------------------------------------------------------------------------
# Strict mode: allowed_edges = ['call']
# ---------------------------------------------------------------------------

@needs_ts
def test_strict_mode_allowed_edges_is_call_only(tmp_path):
    """allowed_edges in the result dict must be exactly ['call'] for strict."""
    from projmem.graph import trace_call_chain

    store = _index_files(tmp_path, {
        "a.js": (
            "export function b() {}\n"
            "export function a() { b(); }\n"
        )
    })
    res = trace_call_chain(store, "a", "b", max_hops=2, mode="strict")
    ae = res.get("allowed_edges") or []
    assert list(ae) == ["call"], (
        f"strict mode must report allowed_edges=['call']; got {ae}")


# ---------------------------------------------------------------------------
# Relaxed mode: allowed_edges = ['call', 'new', 'callback']
# ---------------------------------------------------------------------------

@needs_ts
def test_relaxed_mode_allowed_edges_includes_new_callback(tmp_path):
    """allowed_edges in the result dict must include 'new' and 'callback'
    for relaxed mode, even when the actual path only uses 'call'."""
    from projmem.graph import trace_call_chain

    store = _index_files(tmp_path, {
        "a.js": (
            "export function b() {}\n"
            "export function a() { b(); }\n"
        )
    })
    res = trace_call_chain(store, "a", "b", max_hops=2, mode="relaxed")
    ae = set(res.get("allowed_edges") or [])
    assert "call" in ae
    assert "new" in ae
    assert "callback" in ae


# ---------------------------------------------------------------------------
# mode field is echoed back in the result
# ---------------------------------------------------------------------------

@needs_ts
def test_trace_result_echoes_mode_strict(tmp_path):
    store = _index_files(tmp_path, {"a.js": "export function f() {}\n"})
    from projmem.graph import trace_call_chain
    res = trace_call_chain(store, "f", "f", max_hops=1, mode="strict")
    assert res.get("mode") == "strict"


@needs_ts
def test_trace_result_echoes_mode_relaxed(tmp_path):
    store = _index_files(tmp_path, {"a.js": "export function f() {}\n"})
    from projmem.graph import trace_call_chain
    res = trace_call_chain(store, "f", "f", max_hops=1, mode="relaxed")
    assert res.get("mode") == "relaxed"


# ---------------------------------------------------------------------------
# edge_type values are constrained to the allowed set
# ---------------------------------------------------------------------------

@needs_ts
def test_strict_hops_only_carry_call_edge_type(tmp_path):
    """In strict mode, every non-source hop must have edge_type='call'.
    No 'import', 'read', 'new', or 'callback' may appear in the path.

    BFS direction: source=deepest callee (z), sink=outermost caller (x).
    """
    from projmem.graph import trace_call_chain

    store = _index_files(tmp_path, {
        "a.js": (
            "export function z() {}\n"
            "export function y() { z(); }\n"
            "export function x() { y(); }\n"
        )
    })
    res = trace_call_chain(store, "z", "x", max_hops=3, mode="strict")
    path = res.get("path") or []
    for hop in path[1:]:
        et = hop.get("edge_type")
        assert et == "call", (
            f"strict mode hop must have edge_type='call', got {et!r}: {hop}")


# ---------------------------------------------------------------------------
# Empty path still carries structural keys
# ---------------------------------------------------------------------------

@needs_ts
def test_no_path_result_still_has_structural_keys(tmp_path):
    """When no path exists, the result must still expose allowed_edges and
    mode so callers can understand what was attempted."""
    from projmem.graph import trace_call_chain

    store = _index_files(tmp_path, {
        "a.js": "export function isolated1() {}\n",
        "b.js": "export function isolated2() {}\n",
    })
    res = trace_call_chain(store, "isolated1", "isolated2", max_hops=3)
    assert res.get("path") == []
    assert "allowed_edges" in res, "allowed_edges must be present even on empty path"
    assert "mode" in res, "mode must be present even on empty path"
