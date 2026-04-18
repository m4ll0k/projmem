"""Tests for computed env detection + schema-library extractors +
note-quality guidance.

Real-world failure reported: contract detection too literal — `process.env.FOO`
works but `process.env[key]` / t3-env / envalid / Zod declarations don't.
This made claim verification produce false contradictions on modern TS
projects that use env schemas.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem.ts_backend import available as ts_available
from projmem import claims as _claims


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
    return cfg, store


def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Computed env access detection
# ---------------------------------------------------------------------------

def test_computed_env_bracket_variable(tmp_path):
    """`process.env[key]` (variable, no literal) must emit a computed
    contract entry with role='dynamic-access'."""
    cfg, store = _indexed(tmp_path, {
        "src/a.js": (
            "const key = 'FOO';\n"
            "const x = process.env[key];\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name, role, context FROM contracts "
        "WHERE kind='env' AND file='src/a.js'"))
    store.close()
    computed = [r for r in rows if r["role"] == "dynamic-access"]
    assert computed, (
        f"expected dynamic-access contract for process.env[key]; got {[dict(r) for r in rows]}")
    assert computed[0]["name"] == "__computed__"
    assert "process.env[expr]" in computed[0]["context"]


def test_computed_env_bracket_function_call(tmp_path):
    """`process.env[fn(...)]` also emits a computed contract."""
    cfg, store = _indexed(tmp_path, {
        "src/a.js": (
            "function envName(k) { return 'APP_' + k; }\n"
            "const x = process.env[envName('MODE')];\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name, role FROM contracts "
        "WHERE kind='env' AND file='src/a.js' AND role='dynamic-access'"))
    store.close()
    assert rows, "expected dynamic-access contract for process.env[envName(...)]"


def test_literal_env_does_NOT_trigger_computed(tmp_path):
    """`process.env['FOO']` is a literal bracket; should be handled by
    the literal extractor, NOT the computed one."""
    cfg, store = _indexed(tmp_path, {
        "src/a.js": "const x = process.env['LITERAL_NAME'];\n"
    })
    rows = list(store.conn.execute(
        "SELECT name, role FROM contracts "
        "WHERE kind='env' AND file='src/a.js'"))
    store.close()
    names = {r["name"] for r in rows}
    assert "LITERAL_NAME" in names
    assert "__computed__" not in names


# ---------------------------------------------------------------------------
# Schema-library declarations
# ---------------------------------------------------------------------------

def test_t3_env_schema_detected(tmp_path):
    """`createEnv({ server: { FOO: z.string() } })` registers FOO as a
    schema declaration."""
    cfg, store = _indexed(tmp_path, {
        "src/env.ts": (
            "import { createEnv } from '@t3-oss/env-core';\n"
            "import { z } from 'zod';\n"
            "export const env = createEnv({\n"
            "  APP_MODE: z.string(),\n"
            "  DB_URL: z.string().url(),\n"
            "});\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name, role, context FROM contracts "
        "WHERE kind='env' AND file='src/env.ts'"))
    store.close()
    declared = {r["name"] for r in rows if r["role"] == "declare"}
    assert "APP_MODE" in declared
    assert "DB_URL" in declared


def test_zod_schema_detected(tmp_path):
    """Bare `z.object({ FOO: z.string() })` also picks up."""
    cfg, store = _indexed(tmp_path, {
        "src/env.ts": (
            "import { z } from 'zod';\n"
            "const envSchema = z.object({\n"
            "  API_KEY: z.string(),\n"
            "  LOG_LEVEL: z.enum(['debug', 'info']),\n"
            "});\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name, role FROM contracts "
        "WHERE kind='env' AND file='src/env.ts' AND role='declare'"))
    store.close()
    declared = {r["name"] for r in rows}
    assert "API_KEY" in declared
    assert "LOG_LEVEL" in declared


def test_envalid_schema_detected(tmp_path):
    cfg, store = _indexed(tmp_path, {
        "src/env.ts": (
            "import { cleanEnv, str } from 'envalid';\n"
            "const env = cleanEnv(process.env, {\n"
            "  PORT: str(),\n"
            "  HOST: str(),\n"
            "});\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name, role, context FROM contracts "
        "WHERE kind='env' AND file='src/env.ts' AND role='declare'"))
    store.close()
    declared = {r["name"] for r in rows}
    assert "PORT" in declared
    assert "HOST" in declared


# ---------------------------------------------------------------------------
# Note quality warnings
# ---------------------------------------------------------------------------

def test_verified_env_claim_carries_computed_access_warning(tmp_path):
    """When a literal env claim is VERIFIED but the same file has
    computed access, emit a quality_warning so the agent knows the
    claim isn't exhaustive."""
    cfg, store = _indexed(tmp_path, {
        "src/app.js": (
            "const a = process.env.LITERAL_ONE;\n"
            "const key = 'LITERAL_ONE';\n"
            "const b = process.env[key];\n"  # computed access
        )
    })
    row = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE name='LITERAL_ONE' AND kind='env'").fetchone()
    assert row
    claim = _claims.Claim(
        subject="LITERAL_ONE", predicate="env-read-at",
        object=f"{row['file']}:{row['line']}", truth_class="FACT")
    v = _claims.verify_claim(store, claim)
    store.close()
    assert v.status == _claims.VERIFIED
    codes = {w["code"] for w in v.quality_warnings}
    assert "computed_access_in_file" in codes, (
        f"expected computed_access_in_file warning; got {v.quality_warnings}")


def test_schema_declared_elsewhere_warning(tmp_path):
    """When a literal read-claim is VERIFIED but the env is also
    declared via a schema in another file, surface that as info."""
    cfg, store = _indexed(tmp_path, {
        "src/env.ts": (
            "import { z } from 'zod';\n"
            "const schema = z.object({\n"
            "  APP_MODE: z.string(),\n"
            "});\n"
        ),
        "src/use.js": (
            "const mode = process.env.APP_MODE;\n"
        ),
    })
    row = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE name='APP_MODE' AND kind='env' AND role='read'").fetchone()
    assert row
    claim = _claims.Claim(
        subject="APP_MODE", predicate="env-read-at",
        object=f"{row['file']}:{row['line']}", truth_class="FACT")
    v = _claims.verify_claim(store, claim)
    store.close()
    assert v.status == _claims.VERIFIED
    codes = {w["code"] for w in v.quality_warnings}
    assert "schema_declared_elsewhere" in codes, (
        f"expected schema_declared_elsewhere warning; got {v.quality_warnings}")


def test_no_quality_warnings_when_pure_literal(tmp_path):
    """A literal read-claim with no computed access and no schema
    declaration should have no quality warnings."""
    cfg, store = _indexed(tmp_path, {
        "src/a.js": "const x = process.env.PURE_LITERAL;\n"
    })
    row = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE name='PURE_LITERAL' AND kind='env'").fetchone()
    claim = _claims.Claim(
        subject="PURE_LITERAL", predicate="env-read-at",
        object=f"{row['file']}:{row['line']}", truth_class="FACT")
    v = _claims.verify_claim(store, claim)
    store.close()
    assert v.status == _claims.VERIFIED
    assert v.quality_warnings == []
