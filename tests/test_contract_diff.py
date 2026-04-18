"""Regression tests for the contract-diff engine and obligation projection.

These run on the default (AST + regex) path — no tree-sitter required.
Contract-diff is the substrate for the obligation graph, so it MUST be
reliable on the backend every user gets by default.
"""
from __future__ import annotations
import pathlib
import textwrap

import pytest

from projmem import config as config_mod, indexer
from projmem import contract_diff as cd
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


def _rewrite(root: str, rel: str, body: str) -> None:
    pathlib.Path(root, rel).write_text(textwrap.dedent(body))


def _reindex(root: str, store: Store) -> None:
    cfg = config_mod.load(root)
    indexer.index_all(cfg, store, force=True)


# ---------------------------------------------------------------------------
# Snapshot mechanics
# ---------------------------------------------------------------------------

def test_auto_snapshot_runs_on_every_index(tmp_path):
    """`projmem index` must freeze the previous contract state under
    'pre-index' before wiping. Without this, `contract-diff` has nothing
    to compare against."""
    files = {
        "cli.py": textwrap.dedent("""
            import argparse


            def build():
                p = argparse.ArgumentParser()
                p.add_argument('--verbose', action='store_true')
                return p
        """),
    }
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)
    # pre-index is always present after an index run (may be empty the first time).
    labels = {s["label"] for s in store.list_snapshots()}
    assert "pre-index" in labels, labels
    store.close()


def test_manual_snapshot_is_stable_across_reindex(tmp_path):
    """A user-named snapshot must survive reindex. Only 'pre-index' is
    auto-overwritten; manual labels are pinned until explicitly deleted."""
    files = {"cli.py": "import argparse\n"}
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)
    store.snapshot_contracts("pinned")
    _reindex(root, store)
    labels = {s["label"] for s in store.list_snapshots()}
    assert "pinned" in labels, labels
    store.close()


# ---------------------------------------------------------------------------
# Added contract (flag) — consumer analysis + orphan detection
# ---------------------------------------------------------------------------

def test_added_flag_with_no_consumer_is_orphan(tmp_path):
    """Classic failure: flag declared but nothing reads it. Must appear
    under `added` with `is_orphan=True`."""
    before = {
        "cli.py": textwrap.dedent("""
            import argparse


            def build():
                p = argparse.ArgumentParser()
                p.add_argument('--verbose', action='store_true')
                return p
        """),
    }
    root = _mkpkg(tmp_path, before)
    _cfg, store = _index(root)
    # Capture baseline.
    store.snapshot_contracts("baseline")
    # Add a flag declaration, nothing reads it.
    _rewrite(root, "cli.py", """
        import argparse


        def build():
            p = argparse.ArgumentParser()
            p.add_argument('--verbose', action='store_true')
            p.add_argument('--new-flag', default=None)
            return p
    """)
    _reindex(root, store)
    diff = cd.compute_diff(store, store.snapshot_rows("baseline"),
                           store.live_contract_rows())
    added_names = {(a["kind"], a["name"]) for a in diff["added"]}
    assert ("flag", "new-flag") in added_names, diff["added"]
    nf = next(a for a in diff["added"] if a["name"] == "new-flag")
    ca = nf["consumer_analysis"]
    assert ca["is_orphan"], ca
    # Probe names must include the argparse dest form `new_flag`.
    assert "new_flag" in ca["probe_names"]
    store.close()


def test_added_flag_with_consumer_is_not_orphan(tmp_path):
    """Same flag addition, but this time a consumer file reads it. Must
    NOT be flagged as orphan — the consumer file should appear in
    `consumer_files`."""
    before = {
        "cli.py": textwrap.dedent("""
            import argparse


            def build():
                p = argparse.ArgumentParser()
                p.add_argument('--verbose', action='store_true')
                return p
        """),
        "runner.py": "def run(args):\n    pass\n",
    }
    root = _mkpkg(tmp_path, before)
    _cfg, store = _index(root)
    store.snapshot_contracts("baseline")
    _rewrite(root, "cli.py", """
        import argparse


        def build():
            p = argparse.ArgumentParser()
            p.add_argument('--new-flag', default=None)
            return p
    """)
    _rewrite(root, "runner.py", """
        def run(args):
            if args.new_flag:
                return args.new_flag
    """)
    _reindex(root, store)
    diff = cd.compute_diff(store, store.snapshot_rows("baseline"),
                           store.live_contract_rows())
    nf = next(a for a in diff["added"] if a["name"] == "new-flag")
    ca = nf["consumer_analysis"]
    assert not ca["is_orphan"], ca
    assert "runner.py" in ca["consumer_files"], ca
    store.close()


# ---------------------------------------------------------------------------
# Removed contract — dangling-ref detection
# ---------------------------------------------------------------------------

def test_removed_env_with_surviving_ref_is_dangling(tmp_path):
    """`os.environ['FOO']` in one file, declaration removed, but the
    other file still references it. Dangling-ref must flag this."""
    before = {
        "config.py": "import os\n\nFOO_VALUE = os.environ['FOO']\n",
        "consumer.py": "import os\n\n\ndef f():\n    return os.environ['FOO']\n",
    }
    root = _mkpkg(tmp_path, before)
    _cfg, store = _index(root)
    store.snapshot_contracts("baseline")
    # Remove the config.py declaration; consumer still references FOO.
    _rewrite(root, "config.py", "FOO_VALUE = 'hardcoded'\n")
    _reindex(root, store)
    diff = cd.compute_diff(store, store.snapshot_rows("baseline"),
                           store.live_contract_rows())
    # `FOO` is still declared in consumer.py, so it's not fully removed.
    # But the decl-file list changed — this lands in `moved`, not
    # `removed`. Removed only fires when NOTHING references it any more.
    moved_names = {(m["kind"], m["name"]) for m in diff["moved"]}
    assert ("env", "FOO") in moved_names, diff
    fo = next(m for m in diff["moved"] if m["name"] == "FOO")
    assert "config.py" in fo["removed_files"]
    assert "consumer.py" in fo["kept_files"]
    store.close()


# ---------------------------------------------------------------------------
# Obligation projection
# ---------------------------------------------------------------------------

def test_project_obligations_shape(tmp_path):
    """`project_obligations` must produce one open_obligation per
    orphan-added contract, with the typed `kind` (flag-read, env-read,
    etc.) and the declaration site anchor."""
    before = {"cli.py": "def x(): pass\n"}
    root = _mkpkg(tmp_path, before)
    _cfg, store = _index(root)
    store.snapshot_contracts("baseline")
    _rewrite(root, "cli.py", """
        import argparse


        def build():
            p = argparse.ArgumentParser()
            p.add_argument('--ghost-flag', default=None)
            return p
    """)
    _reindex(root, store)
    diff = cd.compute_diff(store, store.snapshot_rows("baseline"),
                           store.live_contract_rows())
    obs = cd.project_obligations(diff)
    assert obs["coverage_debt"] >= 1, obs
    flag_obs = [o for o in obs["open_obligations"]
                if o["contract"] == "flag:ghost-flag"]
    assert flag_obs, obs
    assert flag_obs[0]["kind"] == "flag-read"
    assert flag_obs[0]["status"] == "open"
    # `declared_at` must preserve file + line of the argparse add_argument.
    assert any(d["file"] == "cli.py" for d in flag_obs[0]["declared_at"])
    store.close()


# ---------------------------------------------------------------------------
# Kind filter
# ---------------------------------------------------------------------------

def test_kind_filter_excludes_token_noise(tmp_path):
    """`token` is excluded by default. A flood of uppercase string
    literals must not poison the diff."""
    before = {"a.py": "X = 'ALPHA'\nY = 'BETA'\n"}
    root = _mkpkg(tmp_path, before)
    _cfg, store = _index(root)
    store.snapshot_contracts("baseline")
    _rewrite(root, "a.py", "X = 'ALPHA'\nY = 'BETA'\nZ = 'GAMMA'\n")
    _reindex(root, store)
    diff = cd.compute_diff(store, store.snapshot_rows("baseline"),
                           store.live_contract_rows())
    # GAMMA is a token — default filter must drop it.
    added_names = {a["name"] for a in diff["added"]}
    assert "GAMMA" not in added_names
    # But if explicitly asked, token appears.
    diff2 = cd.compute_diff(store, store.snapshot_rows("baseline"),
                            store.live_contract_rows(), kinds=["token"])
    names2 = {a["name"] for a in diff2["added"]}
    assert "GAMMA" in names2, diff2
    store.close()
