"""Regression tests for the annotations / notes feature.

Annotations are projmem's answer to context-forgetting between
sessions: a verdict you ASSERT on a target ("verified-safe",
"refute", "documented-footgun", ...) survives reindex and shows up
at the top of `projmem pack <target>` so the next agent reading the
code doesn't re-investigate.

Critical invariants tested here:
  1. add → list → delete round trip works
  2. annotations survive reindex (key is target string, not row id)
  3. annotations on a symbol surface in `pack <symbol>`
  4. annotations on a symbol ALSO surface in `pack <containing-file>`
  5. annotations on a file surface in `pack <file>`
  6. expired annotations are hidden by default, shown with --include-expired
"""
from __future__ import annotations
import pathlib
import textwrap
import time

import pytest

from projmem import config as config_mod, indexer, packs
from projmem.store import Store


def _mkpkg(tmp_path: pathlib.Path, files: dict) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return str(root)


def _index(root: str):
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    return cfg, store


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_add_list_delete_round_trip(tmp_path):
    files = {"a.py": "def helper():\n    return 1\n"}
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)

    ann_id = store.add_annotation(
        target="a.py#helper", kind="verified-safe",
        body="Trivially safe; tested in test_a.py.",
        author="test-session")
    assert isinstance(ann_id, int) and ann_id > 0

    rows = store.list_annotations()
    assert len(rows) == 1
    assert rows[0]["target"] == "a.py#helper"
    assert rows[0]["kind"] == "verified-safe"
    assert rows[0]["author"] == "test-session"

    deleted = store.delete_annotation(ann_id)
    assert deleted is True
    assert store.list_annotations() == []
    store.close()


# ---------------------------------------------------------------------------
# Survives reindex
# ---------------------------------------------------------------------------

def test_annotation_survives_reindex(tmp_path):
    """The whole point of the feature: a verdict written today should
    still be there tomorrow after the codebase is re-indexed. Keying
    on target STRING (not row id, not symbol_id internal) is the
    invariant that delivers this."""
    files = {"a.py": "def helper():\n    return 1\n"}
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    store.add_annotation(target="a.py#helper", kind="note",
                         body="durable verdict")
    store.close()

    # Reindex (forced, simulates re-run)
    indexer.index_all(cfg, Store(cfg.db_path), force=True)

    # Re-open
    store = Store(cfg.db_path)
    rows = store.list_annotations(target="a.py#helper")
    assert len(rows) == 1, "annotation must survive reindex"
    assert rows[0]["body"] == "durable verdict"
    store.close()


# ---------------------------------------------------------------------------
# Surfaces in pack output
# ---------------------------------------------------------------------------

def test_pack_on_symbol_surfaces_annotation(tmp_path):
    files = {"a.py": "def helper():\n    return 1\n"}
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    store.add_annotation(
        target="a.py#helper", kind="verified-safe",
        body="Don't re-audit; covered by tests.",
        author="claude")

    pack = packs.build_pack(cfg, store, "a.py#helper")
    notes = pack.get("human_notes") or []
    assert len(notes) == 1, f"pack should surface the annotation; got {pack.keys()}"
    n = notes[0]
    assert n["kind"] == "verified-safe"
    assert n["confidence"] == "human-asserted"
    assert n["body"].startswith("Don't re-audit")
    assert n["author"] == "claude"
    store.close()


def test_pack_on_file_surfaces_inner_symbol_annotation(tmp_path):
    """If I annotate `file.py#bar`, then pack `file.py` should still
    show that note — because anyone reading the file is interested in
    prior verdicts on its symbols."""
    files = {"a.py": "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"}
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    store.add_annotation(target="a.py#alpha", kind="note",
                         body="alpha is the public entry")
    store.add_annotation(target="a.py#beta", kind="todo",
                         body="rename beta to second")

    pack = packs.build_pack(cfg, store, "a.py")
    notes = pack.get("human_notes") or []
    assert len(notes) == 2
    targets = {n["target"] for n in notes}
    assert targets == {"a.py#alpha", "a.py#beta"}
    store.close()


def test_pack_on_file_surfaces_file_level_annotation(tmp_path):
    files = {"a.py": "def helper():\n    return 1\n"}
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    store.add_annotation(target="a.py", kind="documented-footgun",
                         body="Caller must validate output.")

    pack = packs.build_pack(cfg, store, "a.py")
    notes = pack.get("human_notes") or []
    assert len(notes) == 1
    assert notes[0]["target"] == "a.py"
    assert notes[0]["kind"] == "documented-footgun"
    store.close()


def test_pack_includes_project_and_dir_prefix_annotations(tmp_path):
    files = {
        "pkg/__init__.py": "",
        "pkg/sub/tool.py": "def helper():\n    return 1\n",
    }
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    store.add_annotation(target="@project", kind="note",
                         body="project-wide context")
    store.add_annotation(target="pkg/", kind="note",
                         body="subsystem context", scope="subsystem")
    store.add_annotation(target="pkg/sub/", kind="note",
                         body="nested subsystem context",
                         scope="subsystem")

    pack = packs.build_pack(cfg, store, "pkg/sub/tool.py")
    notes = pack.get("human_notes") or []
    targets = {n["target"] for n in notes}
    assert "@project" in targets
    assert "pkg/" in targets
    assert "pkg/sub/" in targets
    store.close()


def test_pack_on_unknown_symbol_still_surfaces_project_note(tmp_path):
    files = {"a.py": "def helper():\n    return 1\n"}
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    store.add_annotation(target="@project", kind="note",
                         body="project-wide context")

    pack = packs.build_pack(cfg, store, "NotARealSymbol")
    notes = pack.get("human_notes") or []
    assert any(n.get("target") == "@project" for n in notes), notes
    store.close()


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def test_expired_annotations_hidden_by_default(tmp_path):
    files = {"a.py": "def helper():\n    return 1\n"}
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)
    # Set expiry in the past
    past = time.time() - 60
    store.add_annotation(target="a.py", kind="note", body="stale",
                         expires_at=past)
    store.add_annotation(target="a.py", kind="note", body="fresh")

    default_view = store.list_annotations()
    assert len(default_view) == 1
    assert default_view[0]["body"] == "fresh"

    full_view = store.list_annotations(include_expired=True)
    assert len(full_view) == 2
    store.close()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_matches_body_and_target(tmp_path):
    files = {"a.py": "def helper():\n    return 1\n"}
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)
    store.add_annotation(target="a.py#helper", kind="note",
                         body="check the polynomial bounds")
    store.add_annotation(target="a.py", kind="todo", body="add docstrings")

    rows = store.search_annotations("polynomial")
    assert len(rows) == 1
    rows = store.search_annotations("docstring")
    assert len(rows) == 1
    rows = store.search_annotations("helper")  # matches via target
    assert len(rows) == 1
    store.close()
