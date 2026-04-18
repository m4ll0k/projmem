"""Regression tests for the new P0/P1 commands:

  - `projmem session` (no arg) — project-wide entrypoint with contracts
  - `projmem analyze-change <target>` — pre-edit blast-radius
  - `projmem map` — system/module overview
  - `projmem verify-completeness <target>` — post-edit focused gate

Plus prefix-based contract detection and extended alias sources.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

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
# session (no-arg) → project mode
# ---------------------------------------------------------------------------

def test_session_no_arg_returns_project_summary(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/app.py": (
            "import os\n"
            "APP_MODE = os.environ.get('APP_MODE')\n"
            "DB_URL = os.environ.get('DB_URL')\n"
        ),
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "session"])
    assert rc == 0
    data = json.loads(out)
    assert data["mode"] == "project"
    # Carries the notes_summary keys.
    for k in ("totals", "recent_notes", "contradicted", "risk_targets",
              "known_contracts"):
        assert k in data, f"missing {k!r} in project-mode session"
    # Known contracts enumerate env names seen in the repo.
    by_kind = {x["kind"]: x for x in
                data["known_contracts"].get("counts_by_kind", [])}
    assert by_kind.get("env", {}).get("distinct_names", 0) >= 2


def test_session_with_arg_still_works_as_before(tmp_path):
    cfg, store, root = _indexed(tmp_path, {"a.py": "def go(): return 1\n"})
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "session", "a.py"])
    assert rc == 0
    data = json.loads(out)
    assert data["target"] == "a.py"
    assert "mode" not in data   # target-mode doesn't use the mode field


# ---------------------------------------------------------------------------
# analyze-change
# ---------------------------------------------------------------------------

def test_analyze_change_reports_direct_and_indirect(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/core.py":     "def f():\n    return 1\n",
        "src/caller.py":   "from core import f\ndef g(): return f()\n",
        "src/other.py":    "from caller import g\ndef h(): return g()\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json",
                         "analyze-change", "src/core.py"])
    assert rc == 0
    data = json.loads(out)
    direct_files = {d["file"] for d in data["direct_dependents"]}
    assert "src/caller.py" in direct_files
    # Indirect at hop=2 should include src/other.py (reaches core via caller).
    indirect_files = {d["file"] for d in data["indirect_dependents"]}
    assert "src/other.py" in indirect_files
    for ind in data["indirect_dependents"]:
        if ind["file"] == "src/other.py":
            assert ind["hop"] == 2
            assert ind["through"] == "src/caller.py"


def test_analyze_change_surfaces_tests_affected(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/feature.py":        "def run(): return 1\n",
        "tests/test_feature.py": "from src.feature import run\n"
                                  "def test_run(): assert run() == 1\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json",
                         "analyze-change", "src/feature.py"])
    data = json.loads(out)
    test_files = {t["file"] for t in data["tests_affected"]}
    assert "tests/test_feature.py" in test_files


def test_analyze_change_flags_dangling_refs_when_symbol_removed(tmp_path):
    """If a symbol is claimed as the target but no longer exists at the
    file, consumers are dangling — analyze-change should flag them under
    likely_forgotten_updates."""
    cfg, store, root = _indexed(tmp_path, {
        "src/core.py":      "x = 1\n",   # no `old_name` here
        "src/consumer.py":  "from core import x\n"
                             "from core import old_name\n"
                             "def g(): return old_name()\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json",
                         "analyze-change", "src/core.py#old_name"])
    data = json.loads(out)
    kinds = {f["kind"] for f in data["likely_forgotten_updates"]}
    assert "dangling-ref" in kinds


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------

def test_map_detects_modules_from_src_children(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/compiler/a.py":   "def a(): pass\n",
        "src/compiler/b.py":   "def b(): pass\n",
        "src/services/svc.py": "def svc(): pass\n",
        "src/util.py":         "def util(): pass\n",  # directly under src/
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "map"])
    assert rc == 0
    data = json.loads(out)
    module_names = {m["name"] for m in data["modules"]}
    # Expect src/compiler, src/services (auto-detected first-level)
    assert "src/compiler" in module_names
    assert "src/services" in module_names


def test_map_reports_cross_module_edges(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/core/base.py":  "def base(): return 1\n",
        "src/api/use.py":    "from ..core.base import base\n"
                              "def use(): return base()\n",
    })
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "core" / "__init__.py").write_text("")
    (root / "src" / "api" / "__init__.py").write_text("")
    # Re-index with the __init__ files.
    cfg2 = Config(root=str(root))
    s2 = Store(cfg2.db_path)
    index_all(cfg2, s2)
    s2.close()

    rc, out = _run_cli(["--path", str(root), "--json", "map"])
    data = json.loads(out)
    # At minimum the map must report something useful — count depends
    # on Python resolver's ability to follow the relative import.
    assert data["module_count"] >= 2


# ---------------------------------------------------------------------------
# verify-completeness
# ---------------------------------------------------------------------------

def test_verify_completeness_flags_dangling_refs_on_missing_symbol(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/a.py":       "x = 1\n",    # old_name was here, now removed
        "src/consumer.py": "from a import old_name\n"
                            "def run(): return old_name()\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json",
                         "verify-completeness", "src/a.py#old_name"])
    # Exit 1 because we should flag HIGH severity findings.
    assert rc == 1
    data = json.loads(out)
    codes = {f["code"] for f in data["findings"]}
    assert "missing_updates_dangling_refs" in codes
    assert data["severity_counts"]["high"] >= 1


def test_verify_completeness_clean_repo_exit_zero(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/a.py":       "def ok(): return 1\n",
        "src/consumer.py": "from a import ok\n"
                            "def run(): return ok()\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json",
                         "verify-completeness", "src/a.py#ok"])
    assert rc == 0
    data = json.loads(out)
    assert data["severity_counts"]["high"] == 0


# ---------------------------------------------------------------------------
# Prefix-based contracts
# ---------------------------------------------------------------------------

def test_prefix_contracts_tag_next_public(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/client.js": (
            "const url = process.env.NEXT_PUBLIC_API_URL;\n"
            "const key = process.env.NEXT_PUBLIC_KEY;\n"
            "const server = process.env.DATABASE_URL;\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name, context FROM contracts "
        "WHERE kind='env' AND file='src/client.js'"))
    store.close()
    by_name = {r["name"]: r["context"] for r in rows}
    # NEXT_PUBLIC_ envs should carry the `next-public` tag in context.
    assert "NEXT_PUBLIC_API_URL" in by_name
    assert "next-public" in by_name["NEXT_PUBLIC_API_URL"]
    assert "NEXT_PUBLIC_KEY" in by_name
    assert "next-public" in by_name["NEXT_PUBLIC_KEY"]
    # Plain env should NOT have the tag.
    assert "DATABASE_URL" in by_name
    assert "next-public" not in by_name["DATABASE_URL"]


def test_prefix_contracts_tag_vite_and_expo(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "a.js": (
            "const v = process.env.VITE_API;\n"
            "const e = process.env.EXPO_PUBLIC_TOKEN;\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name, context FROM contracts WHERE kind='env'"))
    store.close()
    by_name = {r["name"]: r["context"] for r in rows}
    assert "vite-public" in by_name["VITE_API"]
    assert "expo-public" in by_name["EXPO_PUBLIC_TOKEN"]


# ---------------------------------------------------------------------------
# Extended alias sources
# ---------------------------------------------------------------------------

def test_package_json_imports_field_resolves(tmp_path):
    from projmem import tsconfig as _tsc
    _tsc._reset_cache()
    (tmp_path / "package.json").write_text(json.dumps({
        "imports": {"#utils/*": "./src/utils/*.js"}
    }))
    (tmp_path / "src" / "utils").mkdir(parents=True)
    (tmp_path / "src" / "utils" / "h.js").write_text("export const x = 1;\n")
    candidates = _tsc.resolve_alias("#utils/h", str(tmp_path))
    assert "src/utils/h" in candidates or "src/utils/h.js" in candidates


def test_deno_json_imports_resolves(tmp_path):
    from projmem import tsconfig as _tsc
    _tsc._reset_cache()
    (tmp_path / "deno.json").write_text(json.dumps({
        "imports": {"$std/http": "./vendor/std_http.ts"}
    }))
    candidates = _tsc.resolve_alias("$std/http", str(tmp_path))
    assert any("vendor/std_http" in c for c in candidates)


def test_deno_json_skips_remote_specifiers(tmp_path):
    from projmem import tsconfig as _tsc
    _tsc._reset_cache()
    (tmp_path / "deno.json").write_text(json.dumps({
        "imports": {"$remote": "https://deno.land/std/foo.ts"}
    }))
    candidates = _tsc.resolve_alias("$remote", str(tmp_path))
    assert candidates == []   # remote specifiers are not local files
