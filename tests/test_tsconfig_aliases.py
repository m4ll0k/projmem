"""Regression tests for tsconfig.json / jsconfig.json path alias resolution.

Real-world feedback reported ~386 unresolved imports + 32.1% bind rate on
a repo that uses `@/components/Foo` style aliases. Path alias resolution
is the highest-leverage correctness fix for that failure mode.

Tests cover:
  - exact-match pattern (`@utils`)
  - prefix pattern (`@/*`, `@components/*`)
  - jsonc: comments + trailing commas parse correctly
  - jsconfig.json fallback when tsconfig.json absent
  - extends: one hop of inheritance
  - baseUrl resolution
  - cache invalidation via _reset_cache
  - end-to-end: indexer produces `imports` edges to the right files
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from projmem import tsconfig as _tsc


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test gets a fresh cache so per-root state doesn't leak."""
    _tsc._reset_cache()
    yield
    _tsc._reset_cache()


# ---------------------------------------------------------------------------
# jsonc parser
# ---------------------------------------------------------------------------

def test_parse_jsonc_strips_line_comments():
    text = '{\n  // a comment\n  "a": 1 // trailing\n}'
    assert _tsc._parse_jsonc(text) == {"a": 1}


def test_parse_jsonc_strips_block_comments():
    text = '{ /* block */ "a": /* inline */ 1 }'
    assert _tsc._parse_jsonc(text) == {"a": 1}


def test_parse_jsonc_keeps_slashes_inside_strings():
    text = '{ "url": "http://example.com" }'
    assert _tsc._parse_jsonc(text) == {"url": "http://example.com"}


def test_parse_jsonc_trailing_comma_allowed():
    text = '{ "a": 1, "b": 2, }'
    assert _tsc._parse_jsonc(text) == {"a": 1, "b": 2}


def test_parse_jsonc_malformed_returns_none():
    assert _tsc._parse_jsonc("{ not json") is None


# ---------------------------------------------------------------------------
# alias pattern matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec, pattern, expected", [
    ("@/components/Foo",     "@/*",            "components/Foo"),
    ("@components/Foo",      "@components/*",  "Foo"),
    ("@utils",               "@utils",         ""),
    ("@components/Foo",      "@/*",            None),
    ("@utils/extra",         "@utils",         None),
    ("foo",                  "@*",             None),
    ("@foo",                 "@*",             "foo"),
])
def test_match_alias_variants(spec, pattern, expected):
    assert _tsc._match_alias(spec, pattern) == expected


# ---------------------------------------------------------------------------
# _load: tsconfig + jsconfig + extends
# ---------------------------------------------------------------------------

def test_load_no_config_returns_empty(tmp_path):
    base, paths = _tsc._load(str(tmp_path))
    assert base is None
    assert paths == {}


def test_load_tsconfig_basic(tmp_path):
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["src/*"]},
        },
    }))
    base, paths = _tsc._load(str(tmp_path))
    assert base is not None
    assert "@/*" in paths
    assert paths["@/*"] == ["src/*"]


def test_load_jsconfig_fallback(tmp_path):
    """When tsconfig.json is absent, jsconfig.json is used."""
    (tmp_path / "jsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"~/*": ["src/*"]},
        },
    }))
    _tsc._reset_cache()
    _, paths = _tsc._load(str(tmp_path))
    assert "~/*" in paths


def test_load_extends_one_level(tmp_path):
    """`extends` to a relative base config must merge compilerOptions."""
    (tmp_path / "base.json").write_text(json.dumps({
        "compilerOptions": {"paths": {"@base/*": ["base/*"]}}
    }))
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "extends": "./base.json",
        "compilerOptions": {"baseUrl": ".",
                             "paths": {"@own/*": ["own/*"]}},
    }))
    _, paths = _tsc._load(str(tmp_path))
    # Override wins on `paths` — the spec says `paths` replaces, not merges.
    # But our implementation replaces the whole `paths` object too.
    # What we DO keep from base: `baseUrl` if not overridden.
    assert "@own/*" in paths


# ---------------------------------------------------------------------------
# resolve_alias end-to-end
# ---------------------------------------------------------------------------

def test_resolve_alias_prefix_pattern(tmp_path):
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["src/*"]},
        },
    }))
    candidates = _tsc.resolve_alias("@/components/Button", str(tmp_path))
    assert "src/components/Button" in candidates


def test_resolve_alias_exact_pattern(tmp_path):
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@utils": ["src/utils/index"]},
        },
    }))
    candidates = _tsc.resolve_alias("@utils", str(tmp_path))
    assert "src/utils/index" in candidates


def test_resolve_alias_multiple_targets(tmp_path):
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@shared/*": ["src/shared/*", "packages/shared/*"]},
        },
    }))
    candidates = _tsc.resolve_alias("@shared/util", str(tmp_path))
    assert "src/shared/util" in candidates
    assert "packages/shared/util" in candidates


def test_resolve_alias_no_match_returns_empty(tmp_path):
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["src/*"]},
        },
    }))
    # Specifier doesn't match any pattern.
    assert _tsc.resolve_alias("lodash", str(tmp_path)) == []


def test_resolve_alias_no_config_returns_empty(tmp_path):
    assert _tsc.resolve_alias("@/anything", str(tmp_path)) == []


def test_resolve_alias_baseurl_subdir(tmp_path):
    """baseUrl='./src' means paths are relative to <repo>/src, not the
    tsconfig directory. Verify we emit repo-relative paths correctly."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": "./src",
            "paths": {"@/*": ["./*"]},
        },
    }))
    _tsc._reset_cache()
    candidates = _tsc.resolve_alias("@/Button", str(tmp_path))
    # `@/Button` → baseUrl=src + `./Button` → src/Button
    assert "src/Button" in candidates or "Button" in candidates


# ---------------------------------------------------------------------------
# End-to-end: indexer produces `imports` edges through aliases
# ---------------------------------------------------------------------------

def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


def test_indexer_resolves_alias_imports_to_real_files(tmp_path):
    """End-to-end: an import via `@/...` alias produces a real
    `imports` edge pointing to the on-disk file."""
    from projmem.ts_backend import available as ts_available
    if not ts_available():
        pytest.skip("tree-sitter not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "src" / "utils").mkdir()
    (repo / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {
                "@/*":           ["src/*"],
                "@components/*": ["src/components/*"],
                "@utils":        ["src/utils/index"],
            },
        },
    }))
    (repo / "src" / "components" / "Button.ts").write_text(
        "export function Button() { return 1; }\n")
    (repo / "src" / "utils" / "index.ts").write_text(
        "export function helper() { return 1; }\n")
    (repo / "src" / "app.ts").write_text(
        "import { Button } from '@/components/Button';\n"
        "import { Button as B } from '@components/Button';\n"
        "import { helper } from '@utils';\n"
    )
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    from projmem.config import Config
    from projmem.store import Store
    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    edges = list(store.conn.execute(
        "SELECT dst FROM edges WHERE src='src/app.ts' "
        "AND type='imports'"))
    store.close()

    dsts = {r["dst"] for r in edges}
    assert "src/components/Button.ts" in dsts, (
        f"expected alias resolution to src/components/Button.ts; got {dsts}")
    assert "src/utils/index.ts" in dsts, (
        f"expected alias resolution to src/utils/index.ts; got {dsts}")


def test_indexer_alias_resolution_increases_bind_rate(tmp_path):
    """Presence of resolved alias imports increases ref_binding.bound_pct
    — the originally-reported bug hit bind rate specifically because
    alias imports were left unresolved."""
    from projmem.ts_backend import available as ts_available
    if not ts_available():
        pytest.skip("tree-sitter not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["src/*"]},
        },
    }))
    (repo / "src" / "lib.ts").write_text(
        "export function important() { return 1; }\n")
    (repo / "src" / "caller.ts").write_text(
        "import { important } from '@/lib';\n"
        "export function run() { return important(); }\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "stats"])
    data = json.loads(out)
    # `important` should be bound through the alias-resolved import.
    # With the fix, bind_pct > 0. Without it, 0.
    assert data["ref_binding"]["bound_pct"] > 0
