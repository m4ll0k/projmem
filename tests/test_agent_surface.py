"""Regression tests for the agent-integration surface:

  - `projmem usage`           — markdown + JSON catalog
  - `projmem session <target>` — single-call bootstrap
  - `projmem init`             — write CLAUDE.md / AGENTS.md / .cursorrules

These three commands let any LLM (Claude, GPT, Llama via Ollama,
Mistral, etc.) discover and use projmem without an MCP layer or any
client-specific protocol — all communication is shell argv + JSON
stdout.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from projmem import agent_usage as _u
from projmem import agent_init as _init
from projmem import session as _session
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
# usage — markdown + JSON catalog
# ---------------------------------------------------------------------------

def test_usage_markdown_contains_workflow_and_commands():
    text = _u.render_markdown()
    # Headers and core commands present.
    assert "projmem — usage for AI agents" in text
    assert "session" in text
    assert "audit" in text
    assert "note add --claims" in text
    # Workflow block is present.
    assert "Recommended workflow" in text or "workflow for an agent" in text
    # Bounded — well under 10 KB so it fits in any system message.
    assert len(text) < 10_000


def test_usage_markdown_leads_with_session_not_doctor():
    """Real-world feedback: the intro must point at `projmem session`, not
    at `projmem doctor`. session is the entry point; doctor is tier-2."""
    text = _u.render_markdown()
    # session must appear in the FIRST 600 chars; doctor must not appear
    # before it.
    head = text[:600]
    session_pos = head.find("projmem session")
    doctor_pos = head.find("projmem doctor")
    assert session_pos != -1, (
        f"projmem session must appear in the intro; head={head[:200]!r}")
    if doctor_pos != -1:
        assert session_pos < doctor_pos, (
            "session must precede doctor in the intro")


def test_usage_workflow_calls_out_blockers_explicitly():
    text = _u.render_markdown()
    # The blocker signals must be named so the agent can grep for them.
    for signal in ("freshness_warning", "ambiguity_warning", "contradicted"):
        assert signal in text, f"missing blocker signal {signal!r}"


def test_usage_json_has_entry_point_and_blockers():
    blob = _u.render_json()
    assert blob.get("entry_point") == "projmem session <file_or_symbol>"
    assert "freshness_warning" in blob.get("blockers", [])
    assert "ambiguity_warning" in blob.get("blockers", [])
    assert any("contradicted" in b for b in blob.get("blockers", []))
    assert blob.get("operational_tips"), "expected non-empty operational_tips"


def test_usage_json_catalog_has_required_fields():
    blob = _u.render_json()
    assert blob["tool"] == "projmem"
    assert "thesis" in blob
    assert isinstance(blob["commands"], list)
    assert blob["commands"], "command catalog must be non-empty"
    for cmd in blob["commands"]:
        for k in ("id", "when", "call", "reads"):
            assert k in cmd, f"command missing key {k}: {cmd}"
    assert "predicates_supported" in blob
    assert "env-read-at" in blob["predicates_supported"]


def test_cli_usage_markdown(tmp_path):
    rc, out = _run_cli(["usage"])
    assert rc == 0
    assert "projmem" in out
    assert "session" in out


def test_cli_usage_json(tmp_path):
    rc, out = _run_cli(["--json", "usage"])
    assert rc == 0
    data = json.loads(out)
    assert data["tool"] == "projmem"


# ---------------------------------------------------------------------------
# session — one-call bootstrap
# ---------------------------------------------------------------------------

@needs_ts
def test_session_returns_bounded_blob_with_required_keys(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/app.py": (
            "def handler():\n"
            "    return 1\n"
        ),
    })
    blob = _session.build_session(store, cfg.root, "src/app.py")
    store.close()
    for k in ("schema_version", "target", "doctor_summary",
              "notes_on_target", "notes_total", "integrity",
              "neighbors", "next_steps_hint"):
        assert k in blob, f"missing required key {k!r}"
    assert blob["target"] == "src/app.py"
    assert blob["schema_version"] == 1


@needs_ts
def test_session_surfaces_freshness_warning_on_drift(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/x.py": "x = 1\n",
    })
    # Drift the file on disk after indexing.
    (root / "src" / "x.py").write_text("x = 99999\n")
    blob = _session.build_session(store, cfg.root, "src/x.py")
    store.close()
    assert "freshness_warning" in blob
    assert "Address freshness_warning" in " ".join(blob["next_steps_hint"])


@needs_ts
def test_session_surfaces_refuted_claim(tmp_path):
    """Session must surface refuted_subjects when a prior FACT claim
    no longer holds."""
    from projmem import claims as _claims
    cfg, store, root = _indexed(tmp_path, {
        "src/cfg.py": (
            "import os\n"
            "MODE = os.environ.get('NEW_NAME')\n"
        ),
    })
    # Add a note claiming OLD_NAME at the read site.
    env_row = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE name='NEW_NAME' AND kind='env'").fetchone()
    store.add_annotation(
        target="src/cfg.py", kind="note", body="OLD_NAME read here",
        truth_class="FACT",
        evidence=[{"subject": "OLD_NAME", "predicate": "env-read-at",
                    "object": f"{env_row['file']}:{env_row['line']}",
                    "truth_class": "FACT"}])
    blob = _session.build_session(store, cfg.root, "src/cfg.py")
    store.close()
    # OLD_NAME claim must be REFUTED → refuted_subjects populated.
    assert "OLD_NAME" in blob["refuted_subjects"]
    # Hint must mention investigating the refuted claim.
    hint_text = " ".join(blob["next_steps_hint"])
    assert "refuted" in hint_text


@needs_ts
def test_session_detects_narrow_index_scope(tmp_path):
    """When a target file is in the index but has zero direct + reverse
    deps AND the index contains other files, session must emit a
    `narrow_index_scope` warning + a hint to widen the index."""
    cfg, store, root = _indexed(tmp_path, {
        # Two unrelated files. island.py has no imports/exports and no
        # other file references it.
        "src/island.py":   "ISLAND = 1\n",
        "src/elsewhere.py": "ELSEWHERE = 2\n",
    })
    blob = _session.build_session(store, cfg.root, "src/island.py")
    store.close()
    assert "index_scope_warning" in blob, (
        f"narrow scope must surface index_scope_warning; got "
        f"{list(blob.keys())}")
    assert blob["index_scope_warning"]["code"] == "narrow_index_scope"
    hint_text = " ".join(blob["next_steps_hint"])
    assert "narrow" in hint_text.lower() or "Index scope" in hint_text


@needs_ts
def test_session_does_not_warn_on_single_file_repo(tmp_path):
    """When the index contains only ONE file, zero deps is the truth, not
    a narrow-scope failure. The hint must NOT fire."""
    cfg, store, root = _indexed(tmp_path, {
        "only.py": "x = 1\n",
    })
    blob = _session.build_session(store, cfg.root, "only.py")
    store.close()
    assert "index_scope_warning" not in blob


@needs_ts
def test_session_no_notes_emits_save_hint(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "src/empty_target.py": "x = 1\n"
    })
    blob = _session.build_session(store, cfg.root, "src/empty_target.py")
    store.close()
    assert blob["notes_total"] == 0
    hint_text = " ".join(blob["next_steps_hint"])
    assert "No notes yet" in hint_text


@needs_ts
def test_cli_session_command(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def go():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "session", "a.py"])
    assert rc == 0
    data = json.loads(out)
    assert data["target"] == "a.py"
    assert data["schema_version"] == 1


# ---------------------------------------------------------------------------
# init — write templates into the repo
# ---------------------------------------------------------------------------

def test_init_writes_agents_template_by_default(tmp_path):
    res = _init.write_templates(str(tmp_path), template="agents")
    assert res["wrote"], f"expected to write at least one file: {res}"
    target = tmp_path / "AGENTS.md"
    assert target.exists()
    content = target.read_text()
    # The template's core promise is the three-call primitive set. Any
    # shortening rewrite must preserve these keywords or agents won't
    # find the right workflow.
    assert "projmem task resume" in content
    assert "projmem fact-check" in content
    assert "projmem conclude" in content
    assert "freshness_warning" in content


def test_init_writes_all_templates(tmp_path):
    res = _init.write_templates(str(tmp_path), template="all")
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".cursorrules").exists()
    # `all` now writes the new platform templates too (Gemini, Kiro,
    # Antigravity, Copilot, OpenCode, plus settings.json hooks for
    # Claude/Codex/Gemini). We assert at-least-three so the test
    # doesn't churn every time a platform is added.
    assert len(res["wrote"]) >= 3


def test_init_skips_existing_without_force(tmp_path):
    (tmp_path / "AGENTS.md").write_text("existing content\n")
    res = _init.write_templates(str(tmp_path), template="agents")
    assert not res["wrote"]
    assert res["skipped"]
    # File untouched.
    assert (tmp_path / "AGENTS.md").read_text() == "existing content\n"


def test_init_overwrites_with_force(tmp_path):
    (tmp_path / "AGENTS.md").write_text("existing content\n")
    res = _init.write_templates(str(tmp_path), template="agents", force=True)
    assert res["wrote"]
    text = (tmp_path / "AGENTS.md").read_text()
    assert "projmem task resume" in text
    assert "projmem fact-check" in text


def test_init_unknown_template_returns_error(tmp_path):
    res = _init.write_templates(str(tmp_path), template="bogus")
    assert res["errors"]
    assert "unknown template" in res["errors"][0]["error"]


def test_cli_init_command_does_index_and_templates(tmp_path):
    """`projmem init` is the one-command setup. Builds the index AND
    drops the templates. End-user flow is `pip install projmem &&
    projmem init`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    rc, out = _run_cli(["--path", str(repo), "--json", "init",
                         "--template", "all"])
    assert rc == 0
    data = json.loads(out)
    # Index was built (no prior index DB existed).
    assert data["index"]["action"] == "built"
    assert data["index"].get("indexed", 0) >= 1
    # `all` writes every platform template; ratchet on >= 3 so new
    # platform additions (Gemini, Kiro, Antigravity, etc.) don't churn
    # the assertion.
    assert len(data["templates"]["wrote"]) >= 3
    assert (repo / "AGENTS.md").exists()
    assert (repo / "CLAUDE.md").exists()
    assert (repo / ".cursorrules").exists()
    # next_steps hint surfaced for the user.
    assert data["next_steps"]


def test_cli_init_idempotent_on_rerun(tmp_path):
    """Re-running init must skip both the index rebuild and the existing
    template files. Pure no-op cost."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "y.py").write_text("y = 2\n")
    _run_cli(["--path", str(repo), "--json", "init", "--template", "all"])
    rc, out = _run_cli(["--path", str(repo), "--json", "init",
                         "--template", "all"])
    assert rc == 0
    data = json.loads(out)
    assert "exists" in data["index"]["action"]
    assert data["templates"]["wrote"] == []
    # Every previously-written template gets skipped on the second pass.
    # Ratchet on >= 3 so new platform additions don't churn this assertion.
    assert len(data["templates"]["skipped"]) >= 3


def test_cli_init_no_index_skips_indexing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "z.py").write_text("z = 3\n")
    rc, out = _run_cli(["--path", str(repo), "--json", "init",
                         "--no-index"])
    data = json.loads(out)
    assert data["index"]["action"] == "skipped"


def test_cli_init_reindex_forces_rebuild(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("a = 1\n")
    _run_cli(["--path", str(repo), "--json", "init"])
    rc, out = _run_cli(["--path", str(repo), "--json", "init", "--reindex"])
    data = json.loads(out)
    assert data["index"]["action"] == "rebuilt"


# ---------------------------------------------------------------------------
# refresh — detects modified + added + deleted (the GAP fixed in this round)
# ---------------------------------------------------------------------------

def test_refresh_detects_modified_added_and_deleted(tmp_path):
    """`projmem refresh` was previously blind to NEWLY-ADDED files —
    only flagged modified + deleted. After the fix, all three categories
    surface and `--reindex` applies them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.py").write_text("y = 2\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    # Modify a, delete b, add c.
    (repo / "a.py").write_text("x = 999\n")
    (repo / "b.py").unlink()
    (repo / "c.py").write_text("z = 3\n")

    # Quantum-thinking-round flip: `refresh` now applies by default.
    # Pass `--detect-only` to assert the look-but-don't-touch path.
    rc, out = _run_cli(["--path", str(repo), "--json",
                         "refresh", "--detect-only"])
    assert rc == 0
    data = json.loads(out)
    assert "a.py" in data["modified"]
    assert "b.py" in data["deleted"]
    assert "c.py" in data["added"]
    # `--detect-only` suppresses the apply step.
    assert "applied" not in data


def test_refresh_reindex_applies_all_three_change_types(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.py").write_text("y = 2\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    (repo / "a.py").write_text("x = 999\n")
    (repo / "b.py").unlink()
    (repo / "c.py").write_text("z = 3\n")

    rc, out = _run_cli(["--path", str(repo), "--json",
                         "refresh", "--reindex"])
    assert rc == 0
    data = json.loads(out)
    assert data["applied"]["reindexed"] >= 2  # a + c
    assert data["applied"]["removed"] == 1     # b


# ---------------------------------------------------------------------------
# complete — end-of-task primitive (refresh --reindex + checklist)
# ---------------------------------------------------------------------------

def test_complete_runs_refresh_then_checklist(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    # Add a new file + modify the existing one.
    (repo / "a.py").write_text("def foo():\n    return 999\n")
    (repo / "b.py").write_text("from a import foo\n")

    rc, out = _run_cli(["--path", str(repo), "--json", "complete"])
    assert rc in (0, 1)   # exit code mirrors checklist HIGH-finding count
    data = json.loads(out)
    assert "refresh" in data
    assert "checklist" in data
    assert "next_steps_hint" in data
    # refresh picked up the modify + add.
    assert "a.py" in data["refresh"]["modified"]
    assert "b.py" in data["refresh"]["added"]
    assert data["refresh"]["applied"]["reindexed"] >= 2


def test_complete_first_run_after_init_is_clean(tmp_path):
    """Regression: on a fresh repo, `projmem init` (which builds the index
    AND drops AGENTS.md) followed immediately by `projmem complete` must
    return 0 HIGH findings.

    Prior bug (real-world Codex run): the pre-index snapshot was taken
    BEFORE indexing, so it was empty; after indexing populated contracts,
    the diff `pre-index → current` showed every contract as 'added in
    this session' and checklist flagged 50+ false-positive open
    obligations. Fixed by re-snapshotting pre-index AFTER indexing on
    first-run AND by skipping contract extraction on artifact paths +
    `other`-language files (e.g. AGENTS.md `--flag` mentions)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "import os\n"
        "APP_MODE = os.environ.get('APP_MODE')\n"
        "DB_URL = os.environ.get('DB_URL')\n"
    )

    rc, out = _run_cli(["--path", str(repo), "--json", "init"])
    assert rc == 0
    init_data = json.loads(out)
    assert init_data["index"]["action"] == "built"
    assert init_data["index"].get("indexed", 0) >= 1

    rc, out = _run_cli(["--path", str(repo), "--json", "complete"])
    assert rc == 0, (
        f"first complete after init must exit clean (rc=0); got rc={rc}")
    data = json.loads(out)
    high_count = data["checklist"]["severity_counts"].get("high", 0)
    high_codes = [f["code"] for f in data["checklist"]["findings"]
                   if f["severity"] == "high"]
    assert high_count == 0, (
        f"first complete after init must have 0 HIGH findings; "
        f"got {high_count}: {high_codes}")


def test_complete_clean_repo_emits_all_clear_hint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("def go():\n    return 1\n")
    _run_cli(["--path", str(repo), "index"])
    # No edits between index and complete → no changes.
    rc, out = _run_cli(["--path", str(repo), "--json", "complete"])
    data = json.loads(out)
    assert not data["refresh"]["modified"]
    assert not data["refresh"]["added"]
    assert not data["refresh"]["deleted"]
    hint = " ".join(data["next_steps_hint"])
    assert "All clear" in hint or "No changes" in hint or "no changes" in hint


def test_usage_catalog_contains_complete():
    """`complete` must show up in the usage command catalog so the agent
    discovers it when learning the surface."""
    blob = _u.render_json()
    ids = {c["id"] for c in blob["commands"]}
    assert "complete" in ids


def test_template_mentions_complete():
    """All three templates must instruct the agent to run `projmem
    complete` at task end."""
    pkg = Path(_u.__file__).parent
    for name in ("AGENTS.md", "CLAUDE.md", ".cursorrules"):
        text = (pkg / "templates" / name).read_text()
        assert "projmem complete" in text, (
            f"{name} must mention `projmem complete` for the end-of-task call")
