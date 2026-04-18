"""Regression tests for the repo_memory header signal.

Real-world failure: agents didn't discover prior notes because nothing on
their usual read path surfaced memory presence. A discovery command
existed (`projmem notes`) but required the agent to know to call it.

Fix: every read command appends a `repo_memory` top-level key so the
signal is unavoidable. This test pins the contract.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from projmem import memory_header as _mh
from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem.ts_backend import available as ts_available


def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


def _indexed(tmp_path, files):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    return cfg, store, root


# ---------------------------------------------------------------------------
# Unit: build_header
# ---------------------------------------------------------------------------

def test_build_header_empty_repo(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {"a.py": "x = 1\n"})
    h = _mh.build_header(store)
    store.close()
    assert h["has_memory"] is False
    assert h["total_notes"] == 0
    assert h["contradicted_count"] == 0
    assert h["discover"] == "projmem notes"
    assert "hint" in h
    assert "save concluded facts" in h["hint"].lower()


def test_build_header_with_notes(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {"a.py": "x = 1\n"})
    store.add_annotation(target="a.py", kind="note", body="hello")
    h = _mh.build_header(store)
    store.close()
    assert h["has_memory"] is True
    assert h["total_notes"] == 1
    # The "1 prior note(s)... Run projmem notes ..." hint was removed
    # because it repeated on every read and trained agents to skim past
    # the whole repo_memory block. When nothing demands attention, the
    # header is silent (no `hint` key); the structured fields above
    # carry the signal.
    assert "hint" not in h or not h.get("hint")


def test_build_header_contradicted_elevates_hint(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {"a.py": "x = 1\n"})
    store.add_annotation(target="a.py", kind="note", body="bad",
                          staleness="contradicted")
    h = _mh.build_header(store)
    store.close()
    assert h["contradicted_count"] == 1
    # Hint should mention BLOCKER / contradicted.
    assert "contradicted" in h["hint"].lower()
    assert "blocker" in h["hint"].lower()


# ---------------------------------------------------------------------------
# CLI: every read command returns repo_memory
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd_args", [
    ["symbol", "foo", "--allow-ambiguous", "--no-implicit-check"],
    ["reverse", "a.py"],
    ["forward", "a.py"],
    ["contracts", "a.py", "--kind", "env"],
    ["note-verify", "a.py"],
    ["audit", "a.py"],
    ["integrity", "a.py"],
])
def test_read_commands_emit_repo_memory(tmp_path, cmd_args):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "import os\n"
        "APP = os.environ.get('APP_MODE')\n"
        "def foo():\n"
        "    return APP\n"
    )
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "--json"] + cmd_args)
    assert rc == 0, f"command {cmd_args} failed: {out[:300]}"
    data = json.loads(out)
    assert "repo_memory" in data, (
        f"{cmd_args[0]}: expected repo_memory key; got {list(data.keys())}")
    header = data["repo_memory"]
    for k in ("has_memory", "total_notes", "contradicted_count",
              "recent_activity_7d", "discover"):
        assert k in header, f"{cmd_args[0]}: missing header key {k!r}"


def test_session_emits_repo_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def go(): return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    rc, out = _run_cli(["--path", str(repo), "session", "a.py"])
    data = json.loads(out)
    assert "repo_memory" in data


def test_complete_emits_repo_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    rc, _ = _run_cli(["--path", str(repo), "init"])
    rc, out = _run_cli(["--path", str(repo), "--json", "complete"])
    data = json.loads(out)
    assert "repo_memory" in data


def test_init_output_emits_repo_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    rc, out = _run_cli(["--path", str(repo), "--json", "init"])
    data = json.loads(out)
    assert "repo_memory" in data


# ---------------------------------------------------------------------------
# Default bare `projmem` invocation
# ---------------------------------------------------------------------------

def test_bare_projmem_on_unindexed_repo_tells_user_to_init(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    rc, out = _run_cli(["--path", str(repo)])
    assert rc == 0
    data = json.loads(out)
    assert data["index_present"] is False
    assert data["repo_memory"]["has_memory"] is False
    assert any("projmem init" in s for s in data["next_steps"])


def test_bare_projmem_on_indexed_repo_shows_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    rc, _ = _run_cli(["--path", str(repo), "init"])
    rc, out = _run_cli(["--path", str(repo)])
    assert rc == 0
    data = json.loads(out)
    assert data["index_present"] is True
    assert "repo_memory" in data
    # Quantum-thinking-round: bare-projmem now surfaces a structured
    # `core_verbs` map (the 7 verbs Sonnet actually used across 36
    # benchmark runs) and a hint pointing at the full catalog. The
    # next-steps strings remain but no longer hard-mention specific
    # commands by name.
    assert "core_verbs" in data
    assert "note add" in data["core_verbs"]
    assert "notes"    in data["core_verbs"]
    assert "session"  in data["core_verbs"]
    assert "fact-check" in data["core_verbs"]
    assert data["expert_verbs_count"] >= 1
    assert "projmem usage" in data["expert_verbs_hint"]


def test_bare_projmem_surfaces_contradicted_count(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    rc, _ = _run_cli(["--path", str(repo), "init"])
    # Inject a contradicted note directly.
    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    store.add_annotation(target="x.py", kind="note", body="bad",
                          staleness="contradicted")
    store.close()
    rc, out = _run_cli(["--path", str(repo)])
    data = json.loads(out)
    assert data["repo_memory"]["contradicted_count"] == 1
    # next_steps should call out the contradiction.
    assert any("contradicted" in s for s in data["next_steps"])
