"""Regression tests for projmem/flow.py — contract flow tracing.

Contract:
  - trace_flow() returns a flow_graph list of hops, one per logical step
  - read-site → local-assign detected from source line regex
  - consumer lines tagged as switch-case / switch-head / conditional / read
  - coverage counts split per via-tag
  - CLI `projmem flow <name>` produces the same structure via JSON stdout
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout

import pytest

from projmem import flow as _flow
from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem.ts_backend import available as ts_available

needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


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
# Local-alias detector
# ---------------------------------------------------------------------------

def test_detect_local_alias_js_const():
    line = "const mode = process.env.APP_MODE;"
    alias = _flow._detect_local_alias(line, "APP_MODE")
    assert alias == ("mode", "APP_MODE", "local_var")


def test_detect_local_alias_js_this():
    line = "    this.threshold = process.env.THRESHOLD;"
    alias = _flow._detect_local_alias(line, "THRESHOLD")
    assert alias == ("threshold", "THRESHOLD", "local_var")


def test_detect_local_alias_python_get():
    line = "    api = os.environ.get('API_TOKEN')"
    alias = _flow._detect_local_alias(line, "API_TOKEN")
    assert alias == ("api", "API_TOKEN", "python_env")


def test_detect_local_alias_rejects_wrong_contract():
    line = "const mode = process.env.OTHER_VAR;"
    alias = _flow._detect_local_alias(line, "APP_MODE")
    assert alias is None


def test_detect_local_alias_none_for_inline_use():
    # Inline conditional, no local capture.
    line = "if (process.env.DEBUG) doStuff();"
    alias = _flow._detect_local_alias(line, "DEBUG")
    assert alias is None


def test_detect_local_alias_object_property():
    """Regression: TS code in the wild uses object-literal property
    shorthand, not `const x = ...`. For example,
    `tscWatchFile: process.env.TSC_WATCHFILE,` inside a returned config
    object. The flow tracer must detect this shape too."""
    line = "            tscWatchFile: process.env.TSC_WATCHFILE,"
    alias = _flow._detect_local_alias(line, "TSC_WATCHFILE")
    assert alias == ("tscWatchFile", "TSC_WATCHFILE", "object_property")


def test_detect_local_alias_object_property_with_coercion():
    """Real-world: `useNonPollingWatchers: !!process.env.TSC_NONPOLLING_WATCHER,`"""
    line = "            useNonPollingWatchers: !!process.env.TSC_NONPOLLING_WATCHER,"
    alias = _flow._detect_local_alias(line, "TSC_NONPOLLING_WATCHER")
    assert alias == ("useNonPollingWatchers", "TSC_NONPOLLING_WATCHER",
                      "object_property")


# ---------------------------------------------------------------------------
# Consumer-line classifier
# ---------------------------------------------------------------------------

def test_classify_switch_case():
    tag = _flow._classify_consumer_line('  case "polling":', "mode")
    assert tag == "switch-case"


def test_classify_switch_head():
    tag = _flow._classify_consumer_line("switch (mode) {", "mode")
    assert tag == "switch-head"


def test_classify_conditional():
    tag = _flow._classify_consumer_line("if (mode === 'prod') {", "mode")
    assert tag == "conditional"


def test_classify_plain_read():
    tag = _flow._classify_consumer_line("  const x = mode + 1;", "mode")
    assert tag == "read"


# ---------------------------------------------------------------------------
# Comment-only line filter (limit #5 fix)
# ---------------------------------------------------------------------------

def test_is_comment_only_line_js_double_slash():
    assert _flow._is_comment_only_line("    // tscWatchFile note")


def test_is_comment_only_line_python_hash():
    assert _flow._is_comment_only_line("# x is set elsewhere")


def test_is_comment_only_line_block_open():
    assert _flow._is_comment_only_line("    /* explanation here")


def test_is_comment_only_line_continuation():
    assert _flow._is_comment_only_line("    * keep tscWatchFile aligned")


def test_is_comment_only_line_negative_real_code():
    assert not _flow._is_comment_only_line("    const x = mode + 1;")


def test_is_comment_only_line_negative_trailing_comment():
    """A real expression followed by a trailing comment is NOT comment-only."""
    assert not _flow._is_comment_only_line("    foo(mode); // comment")


# ---------------------------------------------------------------------------
# End-to-end: env var → local → switch
# ---------------------------------------------------------------------------

@needs_ts
def test_trace_flow_env_to_switch(tmp_path):
    """Realistic-shape flow: env-read captured into local, then a switch on
    the local with multiple cases. All hops must appear in flow_graph."""
    cfg, store = _indexed(tmp_path, {
        "src/watch.js": (
            "export function chooseWatcher() {\n"
            "  const mode = process.env.WATCH_MODE;\n"
            "  switch (mode) {\n"
            "    case 'polling': return 'poll';\n"
            "    case 'native':  return 'native';\n"
            "    default:        return 'auto';\n"
            "  }\n"
            "}\n"
        )
    })
    report = _flow.trace_flow(store, cfg.root, "WATCH_MODE", kind="env")
    store.close()

    assert report["subject"] == "WATCH_MODE"
    assert report["coverage"]["read_site_count"] == 1
    assert report["coverage"]["local_alias_count"] == 1
    # We expect the switch head + the 3 case lines to appear as consumers.
    tags = {c["via"] for c in report["consumers"]}
    assert "switch-case" in tags or "switch-head" in tags, (
        f"expected switch tag in consumers: {report['consumers']}")

    # Flow graph must start with env-read, have a local-assign, and end
    # with consumer hops.
    kinds = [hop["kind"] for hop in report["flow_graph"]]
    assert kinds[0] == "env-read"
    assert "local-assign" in kinds


@needs_ts
def test_trace_flow_no_local_alias_inline_use(tmp_path):
    """When a contract is read inline (no captured local), flow still
    reports the read site + enclosing symbol but no local_aliases."""
    cfg, store = _indexed(tmp_path, {
        "src/d.js": (
            "export function dbg() {\n"
            "  if (process.env.DEBUG) console.log('debug');\n"
            "}\n"
        )
    })
    report = _flow.trace_flow(store, cfg.root, "DEBUG", kind="env")
    store.close()
    assert report["coverage"]["read_site_count"] >= 1
    assert report["coverage"]["local_alias_count"] == 0
    # The notes block should mention the inline-use case.
    assert any("No local aliases" in n for n in report["notes"])


@needs_ts
def test_trace_flow_python_os_environ(tmp_path):
    cfg, store = _indexed(tmp_path, {
        "src/cfg.py": (
            "import os\n"
            "def load():\n"
            "    token = os.environ.get('API_TOKEN')\n"
            "    if token:\n"
            "        return token.strip()\n"
            "    return None\n"
        )
    })
    report = _flow.trace_flow(store, cfg.root, "API_TOKEN", kind="env")
    store.close()
    assert report["coverage"]["local_alias_count"] == 1
    locals_captured = {a["local_name"] for a in report["local_aliases"]}
    assert "token" in locals_captured


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

@needs_ts
def test_cli_flow_command(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "x.js").write_text(
        "const level = process.env.LOG_LEVEL;\n"
        "if (level === 'debug') console.log('d');\n"
    )
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "flow", "LOG_LEVEL",
                         "--kind", "env"])
    assert rc == 0
    data = json.loads(out)
    assert data["subject"] == "LOG_LEVEL"
    assert data["coverage"]["read_site_count"] >= 1


@needs_ts
def test_trace_flow_cross_file_object_property(tmp_path):
    """Object-property aliases leak out of the file via the exported object.
    Consumers in importer files must show up as cross-file-read /
    cross-file-destructure hits."""
    cfg, store = _indexed(tmp_path, {
        "src/cfg.js": (
            "export const cfg = {\n"
            "  apiMode: process.env.API_MODE,\n"
            "};\n"
        ),
        "src/handler.js": (
            "import { cfg } from './cfg';\n"
            "export function handle() {\n"
            "  if (cfg.apiMode === 'prod') return 1;\n"
            "  return 0;\n"
            "}\n"
        ),
        "src/log.js": (
            "import { cfg } from './cfg';\n"
            "console.log('mode is', cfg.apiMode);\n"
        ),
    })
    report = _flow.trace_flow(store, cfg.root, "API_MODE", kind="env")
    store.close()

    cross_file = [c for c in report["consumers"]
                  if str(c.get("via", "")).startswith("cross-file")]
    assert cross_file, (
        f"Expected cross-file consumers; got: {report['consumers']}")
    cf_files = {c["file"] for c in cross_file}
    assert "src/handler.js" in cf_files or "src/log.js" in cf_files, (
        f"Cross-file consumers should include importer files; got {cf_files}")
    # Coverage block must include the cross_file_count.
    assert report["coverage"]["cross_file_count"] == len(cross_file)


@needs_ts
def test_trace_flow_no_cross_file_for_local_var(tmp_path):
    """When the alias is a plain local variable (not object-property), the
    cross-file scan must NOT fire — the value doesn't escape."""
    cfg, store = _indexed(tmp_path, {
        "src/a.js": (
            "import { something } from './b';\n"
            "export function f() {\n"
            "  const mode = process.env.LOCAL_MODE;\n"
            "  return something(mode);\n"
            "}\n"
        ),
        "src/b.js": (
            "export function something(x) { return x; }\n"
        ),
    })
    report = _flow.trace_flow(store, cfg.root, "LOCAL_MODE", kind="env")
    store.close()
    cross_file = [c for c in report["consumers"]
                   if str(c.get("via", "")).startswith("cross-file")]
    assert not cross_file, (
        f"local_var alias should not trigger cross-file scan; got {cross_file}")


@needs_ts
def test_trace_flow_filters_comment_only_lines(tmp_path):
    """Comment-only lines mentioning the local name must NOT show up as
    consumers — they aren't real reads."""
    cfg, store = _indexed(tmp_path, {
        "src/x.js": (
            "export function f() {\n"
            "  const mode = process.env.MY_MODE;\n"
            "  // mode is set above — see tests for behaviour\n"
            "  return mode;\n"
            "}\n"
        )
    })
    report = _flow.trace_flow(store, cfg.root, "MY_MODE", kind="env")
    store.close()
    # The comment line on line 3 must NOT appear; the real read on line 4
    # must.
    consumer_lines = [c["line"] for c in report["consumers"]]
    assert 3 not in consumer_lines, (
        f"comment-only line 3 should be filtered; got {consumer_lines}")
    assert 4 in consumer_lines


@needs_ts
def test_trace_flow_consumers_carry_confidence_field(tmp_path):
    """Each consumer hit must carry `ast_confirmed` + `confidence` so the
    agent can prioritize. AST-confirmed hits are higher trust."""
    cfg, store = _indexed(tmp_path, {
        "src/y.js": (
            "export function g() {\n"
            "  const mode = process.env.MODE2;\n"
            "  doStuff(mode);\n"
            "}\n"
        )
    })
    report = _flow.trace_flow(store, cfg.root, "MODE2", kind="env")
    store.close()
    assert report["consumers"], "expected at least one consumer"
    for c in report["consumers"]:
        assert "ast_confirmed" in c
        assert c.get("confidence") in ("high", "medium")


@needs_ts
def test_trace_flow_no_cross_file_flag_disables_scan(tmp_path):
    """cross_file=False must suppress the importer scan even for
    object_property aliases."""
    cfg, store = _indexed(tmp_path, {
        "src/cfg.js": (
            "export const cfg = { mode: process.env.X };\n"
        ),
        "src/uses.js": (
            "import { cfg } from './cfg';\n"
            "console.log(cfg.mode);\n"
        ),
    })
    report = _flow.trace_flow(store, cfg.root, "X", kind="env",
                               cross_file=False)
    store.close()
    cross_file = [c for c in report["consumers"]
                   if str(c.get("via", "")).startswith("cross-file")]
    assert not cross_file


@needs_ts
def test_cli_flow_missing_contract_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text("const x = 1;\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "flow", "NONEXISTENT",
                         "--kind", "env"])
    data = json.loads(out)
    assert data["coverage"]["read_site_count"] == 0


def test_flow_java_bean_setter_surfaces_call_sites(tmp_path):
    """Java Bean-setter pattern (`obj.setAllowBackslash(true)`) is the
    Tomcat-style flag-flip surface. The flow trace must enumerate every
    call site as a consumer with `via=setter-call` and a `setter_value`
    field showing the literal arg when it's a boolean — and the SETTER
    DEFINITION itself (`public void setAllowBackslash(boolean v)`) must
    NOT appear as a consumer (declarations aren't writes)."""
    cfg, store = _indexed(tmp_path, {
        "Connector.java": (
            "package org.apache;\n"
            "public class Connector {\n"
            "    public void setAllowBackslash(boolean v) "
            "{ this.allowBackslash = v; }\n"
            "    public boolean allowBackslash = false;\n"
            "}\n"
        ),
        "Config.java": (
            "package org.apache;\n"
            "public class Config {\n"
            "    public void configure() {\n"
            "        Connector connector = new Connector();\n"
            "        connector.setAllowBackslash(true);\n"
            "    }\n"
            "    public void strict() {\n"
            "        Connector c = new Connector();\n"
            "        c.setAllowBackslash(false);\n"
            "    }\n"
            "}\n"
        ),
    })
    report = _flow.trace_flow(store, cfg.root, "allowBackslash",
                                kind="flag", cross_file=False)
    store.close()
    setter_calls = [c for c in report["consumers"]
                    if c["via"] == "setter-call"]
    assert len(setter_calls) == 2, (
        f"expected 2 setter-call consumers (Config.configure + .strict); "
        f"got {len(setter_calls)}: {setter_calls}")
    by_value = {c["setter_value"] for c in setter_calls}
    assert by_value == {"true", "false"}, (
        f"expected boolean values surfaced; got {by_value}")
    assert report["coverage"]["setter_call_count"] == 2
    # Method declaration must NOT appear as a consumer.
    decl_lines = [c for c in setter_calls
                  if c["file"] == "Connector.java" and c["line"] == 3]
    assert not decl_lines, (
        f"setter declaration leaked into consumers: {decl_lines}")


def test_flow_xml_property_declares_flag(tmp_path):
    """Tomcat / Spring XML config (`<property name="allowBackslash"/>`) is
    the third surface the same flag lives on. We index it as a
    `declare`-role flag contract so flow surfaces the XML site as a
    read_site even when no Java setter call exists."""
    cfg, store = _indexed(tmp_path, {
        "conf.xml": (
            "<server>\n"
            "  <Connector>\n"
            "    <property name=\"allowBackslash\">true</property>\n"
            "  </Connector>\n"
            "</server>\n"
        ),
    })
    report = _flow.trace_flow(store, cfg.root, "allowBackslash",
                                kind="flag", cross_file=False)
    store.close()
    xml_sites = [s for s in report["read_sites"]
                 if s["file"] == "conf.xml"]
    assert xml_sites, f"XML declare site missing; read_sites={report['read_sites']}"
    assert xml_sites[0]["role"] == "declare"
