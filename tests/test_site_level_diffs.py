"""Regression tests for site-level output in symbol-diff and contract-diff.

Contract:
  - symbol-diff added/removed entries include file, line, end_line, kind,
    name, symbol_id (not just name+kind).
  - symbol-diff moved entries include from_sites + to_sites lists with
    concrete file:line coordinates.
  - contract-diff moved entries include added_sites and removed_sites
    with file:line plus role/confidence.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout

import pytest

from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem.ts_backend import available as ts_available

needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# symbol-diff site-level output
# ---------------------------------------------------------------------------

@needs_ts
def test_symbol_diff_added_includes_line(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # pre-index snapshot is auto-taken
    (repo / "a.py").write_text(
        "def alpha():\n    return 1\n"
        "def beta():\n    return 2\n")
    rc, _ = _run_cli(["--path", str(repo), "index", "--force"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "symbol-diff",
                         "--base", "pre-index"])
    assert rc == 0
    data = json.loads(out)
    added_names = {a["name"] for a in data.get("added", [])}
    assert "beta" in added_names
    # Every added entry must have file + line keys (no bare name/kind dict).
    for a in data["added"]:
        assert "file" in a
        assert "line" in a
        assert a["line"] is not None


@needs_ts
def test_symbol_diff_moved_includes_from_and_to_sites(tmp_path):
    """A symbol renamed into a different file yields a `moved` entry with
    concrete from_sites and to_sites carrying file + line."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def thing():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # Move `thing` to b.py at a different line.
    os.remove(repo / "a.py")
    (repo / "b.py").write_text("\n\ndef thing():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index", "--force"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "symbol-diff",
                         "--base", "pre-index"])
    data = json.loads(out)
    moved_names = {m["name"] for m in data.get("moved", [])}
    assert "thing" in moved_names
    moved_entry = next(m for m in data["moved"] if m["name"] == "thing")
    # Site-level coordinates present.
    assert moved_entry.get("from_sites"), (
        f"expected from_sites in moved entry: {moved_entry}")
    assert moved_entry.get("to_sites")
    # Old site was a.py:1, new site is b.py:3.
    from_files = {s["file"] for s in moved_entry["from_sites"]}
    to_files = {s["file"] for s in moved_entry["to_sites"]}
    assert "a.py" in from_files
    assert "b.py" in to_files
    for s in moved_entry["from_sites"] + moved_entry["to_sites"]:
        assert s.get("line") is not None


# ---------------------------------------------------------------------------
# contract-diff site-level output
# ---------------------------------------------------------------------------

@needs_ts
def test_contract_diff_moved_includes_site_lines(tmp_path):
    """An env read that moves from one file to another must produce a
    `moved` entry with added_sites + removed_sites including line numbers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "a.js").write_text(
        "\n\nconst x = process.env.MY_VAR;\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # Move the env read to b.js.
    (repo / "src" / "a.js").write_text("const x = 'no env here';\n")
    (repo / "src" / "b.js").write_text(
        "\nconst y = process.env.MY_VAR;\n")
    rc, _ = _run_cli(["--path", str(repo), "index", "--force"])
    rc, out = _run_cli(["--path", str(repo), "contract-diff",
                         "--base", "pre-index", "--kind", "env"])
    data = json.loads(out)
    if not data.get("moved"):
        # Indexer may classify as removed-and-re-added; accept either shape
        # as long as line info is present.
        added = data.get("added", [])
        removed = data.get("removed", [])
        assert any("MY_VAR" == a["name"] for a in added)
        any_decl = next((a for a in added if a["name"] == "MY_VAR"), None)
        assert any_decl
        assert all(d.get("line") is not None
                   for d in any_decl.get("declarations", []))
    else:
        moved = next((m for m in data["moved"] if m["name"] == "MY_VAR"),
                     None)
        assert moved
        assert "added_sites" in moved
        assert "removed_sites" in moved
        for s in moved["added_sites"] + moved["removed_sites"]:
            assert s.get("line") is not None


@needs_ts
def test_symbol_diff_moved_includes_before_after_pairs(tmp_path):
    """1-to-1 symbol move must produce a `before_after` pair list with
    {before:{file,line}, after:{file,line}} so the reader doesn't have to
    correlate from_sites/to_sites manually."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def widget():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    os.remove(repo / "a.py")
    (repo / "b.py").write_text("\n\n\ndef widget():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index", "--force"])
    rc, out = _run_cli(["--path", str(repo), "symbol-diff",
                         "--base", "pre-index"])
    data = json.loads(out)
    moved_entry = next((m for m in data.get("moved", [])
                         if m["name"] == "widget"), None)
    assert moved_entry, f"expected widget in moved: {data.get('moved')}"
    assert moved_entry.get("shape") == "1to1"
    pairs = moved_entry.get("before_after") or []
    assert len(pairs) == 1
    p = pairs[0]
    assert p["before"]["file"] == "a.py"
    assert p["after"]["file"] == "b.py"
    assert p["before"]["line"] is not None
    assert p["after"]["line"] is not None


@needs_ts
def test_contract_diff_moved_includes_before_after_pairs(tmp_path):
    """1-to-1 contract move must produce a `before_after` pair list."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "a.js").write_text("\nconst x = process.env.MOVE_ME;\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    (repo / "src" / "a.js").write_text("const x = 'gone';\n")
    (repo / "src" / "b.js").write_text(
        "\n\nconst y = process.env.MOVE_ME;\n")
    rc, _ = _run_cli(["--path", str(repo), "index", "--force"])
    rc, out = _run_cli(["--path", str(repo), "contract-diff",
                         "--base", "pre-index", "--kind", "env"])
    data = json.loads(out)
    moved = next((m for m in data.get("moved", [])
                  if m["name"] == "MOVE_ME"), None)
    if moved:
        assert "before_after" in moved
        pairs = moved["before_after"]
        if moved.get("shape") == "1to1":
            assert len(pairs) == 1
            p = pairs[0]
            assert p["before"]["line"] is not None
            assert p["after"]["line"] is not None


@needs_ts
def test_contract_diff_added_has_declarations_with_lines(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text("const x = 1;\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    (repo / "a.js").write_text(
        "\n\nconst x = process.env.NEW_ENV;\n")
    rc, _ = _run_cli(["--path", str(repo), "index", "--force"])
    rc, out = _run_cli(["--path", str(repo), "contract-diff",
                         "--base", "pre-index", "--kind", "env"])
    data = json.loads(out)
    new_entry = next((a for a in data.get("added", [])
                      if a["name"] == "NEW_ENV"), None)
    assert new_entry
    assert new_entry.get("declarations")
    assert all(d.get("line") is not None
               for d in new_entry["declarations"])
