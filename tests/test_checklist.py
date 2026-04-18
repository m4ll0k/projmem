"""Regression tests for `projmem checklist` (post-edit completeness gate)."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from projmem.cli import main


def _run_cli(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


def test_checklist_empty_db_returns_no_index_error(tmp_path):
    """Round-4 #3: any read command on an unindexed root must produce a
    structured `no-index` error (exit 2), not silent zeros and not a
    misleading "missing snapshot" finding. Indexing is the prerequisite,
    not an optional input."""
    root = tmp_path / "repo"
    root.mkdir()
    try:
        rc, out = _run_cli(["--path", str(root), "checklist"])
    except SystemExit as e:
        rc = e.code
        out = ""  # captured separately
    # We reach via SystemExit(2) with the error payload printed.
    import io
    import sys
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            from projmem.cli import main
            main(["--path", str(root), "checklist"])
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    data = json.loads(buf.getvalue())
    assert rc == 2
    assert data["error"] == "no-index"
    assert root.name in data["message"] or str(root) in data["message"]


def test_checklist_flags_dangling_symbol_refs_after_rename(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "b.py").write_text(
        "def old():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (root / "a.py").write_text(
        "from b import old\n\n"
        "def call():\n"
        "    return old()\n",
        encoding="utf-8",
    )
    rc, _ = _run_cli(["--path", str(root), "index"])
    assert rc == 0

    # Rename old → new but forget to update a.py call site.
    (root / "b.py").write_text(
        "def new():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    rc, _ = _run_cli(["--path", str(root), "index", "--force"])
    assert rc == 0

    rc, out = _run_cli(["--path", str(root), "checklist"])
    data = json.loads(out)
    assert rc == 1
    dangling = next((f for f in data["findings"]
                     if f["code"] == "dangling_symbol_refs"), None)
    assert dangling, data["findings"]
    # Expect the old name to show up with a ref in a.py.
    names = {d["name"] for d in dangling["details"]["dangling"]}
    assert "old" in names
    old_entry = next(d for d in dangling["details"]["dangling"]
                     if d["name"] == "old")
    assert any(s["file"] == "a.py" for s in old_entry["example_sites"])


def test_checklist_flags_open_contract_obligations_for_orphan_flag(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "cli.py").write_text(
        "import argparse\n\n"
        "def main(argv=None):\n"
        "    p = argparse.ArgumentParser()\n"
        "    return p.parse_args(argv)\n",
        encoding="utf-8",
    )
    rc, _ = _run_cli(["--path", str(root), "index"])
    assert rc == 0

    # Add a new flag declaration but no consumer.
    (root / "cli.py").write_text(
        "import argparse\n\n"
        "def main(argv=None):\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--new-flag')\n"
        "    return p.parse_args(argv)\n",
        encoding="utf-8",
    )
    rc, _ = _run_cli(["--path", str(root), "index", "--force"])
    assert rc == 0

    rc, out = _run_cli(["--path", str(root), "checklist"])
    data = json.loads(out)
    assert rc == 1
    finding = next((f for f in data["findings"]
                    if f["code"] == "open_contract_obligations"), None)
    assert finding, data["findings"]
    obs = finding["details"]["open_obligations"]
    assert any(o.get("contract") == "flag:new-flag" for o in obs), obs

