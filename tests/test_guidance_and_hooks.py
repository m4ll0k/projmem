"""Step 2 coverage — guidance/constraint/preference kinds + severity column,
`projmem context` verb, Claude Code hook installer, implicit lease path."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from projmem import hook_templates, mutation_verbs as mv
from projmem.store import Store


GOOD_REASON = "extracting auth into shared/jwt.py"


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / ".projmem" / "index.db"))
    yield s
    s.close()


def _seed(store, path):
    store.upsert_file(path, "py", "h", time.time(), 1, "ast")
    store.conn.commit()


# ---------------------------------------------------------------------------
# v2 note kinds + severity column
# ---------------------------------------------------------------------------

class TestGuidanceKinds:
    def test_severity_column_exists(self, store):
        rows = store.conn.execute("PRAGMA table_info(annotations)").fetchall()
        names = [r[1] for r in rows]
        assert "severity" in names

    def test_add_annotation_persists_severity(self, store):
        _seed(store, "src/a.py")
        ann_id = store.add_annotation(
            target="src/a.py", kind="guidance",
            body="prefer functional style here",
            severity="warn",
        )
        row = store.conn.execute(
            "SELECT severity FROM annotations WHERE id=?", (ann_id,),
        ).fetchone()
        assert row["severity"] == "warn"


# ---------------------------------------------------------------------------
# `projmem context` — read-only guidance bundle
# ---------------------------------------------------------------------------

def _add_note(store, target, body, *, kind="note", staleness="fresh"):
    cur = store.conn.execute(
        "INSERT INTO annotations(target, kind, body, created_at, staleness) "
        "VALUES(?, ?, ?, ?, ?)",
        (target, kind, body, time.time(), staleness),
    )
    store.conn.commit()
    return cur.lastrowid


class TestContextForPath:
    def test_returns_file_dir_and_project_notes(self, store):
        _seed(store, "src/auth/jwt.py")
        _add_note(store, "src/auth/jwt.py", "file-scoped guidance")
        _add_note(store, "src/auth/",       "dir-scoped guidance")
        _add_note(store, "@project",        "project-wide guidance")
        result = mv.context_for_path(store, "src/auth/jwt.py")
        bodies = {g["body"] for g in result["guidance"]}
        assert {"file-scoped guidance", "dir-scoped guidance",
                 "project-wide guidance"} <= bodies

    def test_drops_contradicted_by_default(self, store):
        _seed(store, "src/a.py")
        _add_note(store, "src/a.py", "still good", staleness="fresh")
        _add_note(store, "src/a.py", "refuted",    staleness="contradicted")
        result = mv.context_for_path(store, "src/a.py")
        kept = [g["body"] for g in result["guidance"]]
        dropped = [g["body"] for g in result["stale_excluded"]]
        assert "still good" in kept
        assert "refuted" not in kept
        assert "refuted" in dropped

    def test_include_stale_returns_them(self, store):
        _seed(store, "src/a.py")
        _add_note(store, "src/a.py", "refuted", staleness="contradicted")
        result = mv.context_for_path(store, "src/a.py", include_stale=True)
        kept = [g["body"] for g in result["guidance"]]
        assert "refuted" in kept


# ---------------------------------------------------------------------------
# Implicit lease (PostToolUse without PreToolUse)
# ---------------------------------------------------------------------------

class TestImplicitLease:
    def test_open_implicit_lease_creates_open_state(self, store):
        result = mv.open_implicit_lease(store, "src/sneaky.py")
        row = store.conn.execute(
            "SELECT agent_id, state FROM edit_lease WHERE id=?",
            (result["lease_id"],),
        ).fetchone()
        assert row["agent_id"] == "implicit"
        assert row["state"] == "open"

    def test_implicit_lease_closes_via_done(self, store):
        result = mv.open_implicit_lease(store, "src/sneaky.py")
        closed = mv.close_lease(store, result["lease_id"], kind="done")
        assert closed["closed_kind"] == "done"

    def test_find_open_lease_returns_active_one(self, store):
        _seed(store, "src/x.py")
        lease = mv.open_editing_lease(store, "src/x.py", reason=GOOD_REASON)
        found = mv.find_open_lease_for_path(store, "src/x.py")
        assert found is not None
        assert found["id"] == lease["lease_id"]

    def test_find_open_lease_returns_none_when_closed(self, store):
        _seed(store, "src/x.py")
        lease = mv.open_editing_lease(store, "src/x.py", reason=GOOD_REASON)
        mv.close_lease(store, lease["lease_id"], kind="done")
        assert mv.find_open_lease_for_path(store, "src/x.py") is None


# ---------------------------------------------------------------------------
# Hook installer
# ---------------------------------------------------------------------------

def _run_cli(args, *, cwd):
    return subprocess.run(
        [sys.executable, "-m", "projmem.cli", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )


class TestHookInstaller:
    def test_dry_run_does_not_write_anything(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        rc = _run_cli(
            ["hook", "install", "--claude-code", "--dry-run", "--json"],
            cwd=repo,
        )
        assert rc.returncode == 0, rc.stderr
        out = json.loads(rc.stdout)
        assert out["dry_run"] is True
        assert not (repo / ".claude" / "hooks" / "pre-tool-use.py").exists()

    def test_install_writes_executable_scripts(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        rc = _run_cli(
            ["hook", "install", "--claude-code", "--json"], cwd=repo,
        )
        assert rc.returncode == 0, rc.stderr
        out = json.loads(rc.stdout)
        for written in out["written"]:
            assert os.path.isfile(written)
            assert os.access(written, os.X_OK), "hook script must be executable"

    def test_status_reports_presence_and_template_match(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_cli(
            ["hook", "install", "--claude-code", "--json"], cwd=repo,
        )
        rc = _run_cli(["hook", "status", "--claude-code", "--json"], cwd=repo)
        assert rc.returncode == 0
        status = json.loads(rc.stdout)
        assert status["pre_present"] is True
        assert status["post_present"] is True
        assert status["pre_matches_template"] is True
        assert status["post_matches_template"] is True

    def test_uninstall_removes_scripts(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_cli(["hook", "install", "--claude-code", "--json"], cwd=repo)
        _run_cli(["hook", "uninstall", "--claude-code", "--json"], cwd=repo)
        assert not (repo / ".claude" / "hooks" / "pre-tool-use.py").exists()
        assert not (repo / ".claude" / "hooks" / "post-tool-use.py").exists()


# ---------------------------------------------------------------------------
# Shell-injection safety — note bodies must NEVER reach a shell
# ---------------------------------------------------------------------------

class TestHookShellSafety:
    """The brief calls out treating note bodies as data only — a note
    containing ``$(rm -rf /)`` must NOT execute. We assert two
    properties: (1) the hook template never uses ``shell=True`` or
    ``os.system``; (2) running PreToolUse on a real note carrying a
    shell-payload body does not execute the payload.
    """

    def test_template_never_uses_shell_true(self):
        assert "shell=True" not in hook_templates.PRE_TOOL_USE_SCRIPT
        assert "shell=True" not in hook_templates.POST_TOOL_USE_SCRIPT
        assert "os.system" not in hook_templates.PRE_TOOL_USE_SCRIPT
        assert "os.system" not in hook_templates.POST_TOOL_USE_SCRIPT
        # ``eval`` would also be a smell; not used in either template.
        assert " eval(" not in hook_templates.PRE_TOOL_USE_SCRIPT
        assert " eval(" not in hook_templates.POST_TOOL_USE_SCRIPT

    def test_pretooluse_does_not_execute_shell_payload_in_note_body(
        self, tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("def foo():\n    return 1\n")
        # Build the index so PreToolUse has something to talk to.
        rc = _run_cli(["index"], cwd=repo)
        assert rc.returncode == 0, rc.stderr
        # Plant a guidance note whose body is a shell-injection payload.
        canary = tmp_path / "canary.touched"
        payload = f"$(touch {canary})"
        rc = _run_cli(
            ["note", "add", "a.py", "--kind", "guidance",
             "--severity", "warn", payload],
            cwd=repo,
        )
        assert rc.returncode == 0, rc.stderr

        # Install + invoke the PreToolUse script with a synthetic payload.
        _run_cli(["hook", "install", "--claude-code", "--json"], cwd=repo)
        hook_path = repo / ".claude" / "hooks" / "pre-tool-use.py"
        hook_input = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": "a.py"},
            "cwd": str(repo),
        })
        result = subprocess.run(
            [sys.executable, str(hook_path)],
            input=hook_input, capture_output=True, text=True, timeout=15,
        )
        # The hook itself must exit 0 regardless.
        assert result.returncode == 0
        # The injection payload must NOT have executed — `touch` would
        # have created the canary file.
        assert not canary.exists(), (
            f"shell payload from note body executed; canary at {canary} "
            f"was created — this is a critical CVE-class regression."
        )
        # And the body should appear in the additionalContext as inert text.
        try:
            blob = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            blob = {}
        ctx = (
            (blob.get("hookSpecificOutput") or {})
            .get("additionalContext") or ""
        )
        # The literal payload string should be visible as guidance — that
        # proves we treated it as data, not as code.
        assert payload in ctx or payload[:40] in ctx


# ---------------------------------------------------------------------------
# PreToolUse enforcement — Bash detection + permissionDecision="deny"
# This is the load-bearing fix for "agent ignores CLAUDE.md and rm -rf's
# a file that has a critical note attached". The hook now BLOCKS, not
# just nudges.
# ---------------------------------------------------------------------------

class TestPreToolUseEnforcement:
    def _setup_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("def foo():\n    return 1\n")
        (repo / "b.py").write_text("def bar():\n    return 2\n")
        rc = _run_cli(["index"], cwd=repo)
        assert rc.returncode == 0, rc.stderr
        _run_cli(["hook", "install", "--claude-code", "--json"], cwd=repo)
        return repo

    def _invoke_hook(self, repo, payload):
        hook_path = repo / ".claude" / "hooks" / "pre-tool-use.py"
        return subprocess.run(
            [sys.executable, str(hook_path)],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=15,
        )

    def test_bash_rm_on_excluded_path_is_denied(self, tmp_path):
        # The user's exact case: agent runs `rm -rf a.py`, projmem has
        # an exclusion on the dir/file → hook must DENY before the rm
        # actually happens.
        repo = self._setup_repo(tmp_path)
        rc = _run_cli([
            "note", "add", "a.py", "--kind", "exclude",
            "do not touch — load-bearing reducer",
        ], cwd=repo)
        assert rc.returncode == 0, rc.stderr

        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf a.py"},
            "cwd": str(repo),
        })
        assert result.returncode == 0
        blob = json.loads(result.stdout)
        out = blob["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "deny", out
        reason = out["permissionDecisionReason"]
        assert "OUT OF SCOPE" in reason
        assert "a.py" in reason

    def test_bash_rm_on_unguarded_path_passes_through(self, tmp_path):
        # The hook must NOT block routine operations on files that
        # have no critical/exclusion guard — otherwise it'd be useless.
        repo = self._setup_repo(tmp_path)
        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {"command": "rm b.py"},
            "cwd": str(repo),
        })
        assert result.returncode == 0
        # No deny — either no output, or context-only output without
        # a permissionDecision key.
        if result.stdout.strip():
            blob = json.loads(result.stdout)
            out = blob.get("hookSpecificOutput") or {}
            assert out.get("permissionDecision") != "deny"

    def test_edit_on_critical_blocked_file_is_denied(self, tmp_path):
        # Same enforcement on the Edit tool. Agents that hallucinate
        # past CLAUDE.md and call Edit anyway hit the same deny.
        repo = self._setup_repo(tmp_path)
        long_reason = (
            "load-bearing reducer — never modify without first reading "
            "the docstring; multiple downstream consumers depend on "
            "the exact output shape and an incident in 2025 cost us a "
            "week of work."
        )
        rc = _run_cli([
            "critical", "add", "a.py",
            "--reason", long_reason,
            "--category", "data_integrity",
            "--self-cosign",
        ], cwd=repo)
        assert rc.returncode == 0, rc.stderr

        result = self._invoke_hook(repo, {
            "tool_name": "Edit",
            "tool_input": {"file_path": "a.py"},
            "cwd": str(repo),
        })
        assert result.returncode == 0
        blob = json.loads(result.stdout)
        out = blob["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "CRITICAL" in out["permissionDecisionReason"]

    def test_bash_mv_picks_up_source_path(self, tmp_path):
        # `mv a.py renamed.py` should check a.py since that's the
        # source the user wants to move/remove.
        repo = self._setup_repo(tmp_path)
        _run_cli([
            "note", "add", "a.py", "--kind", "exclude",
            "frozen — depends on exact bytes",
        ], cwd=repo)
        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {"command": "mv a.py renamed.py"},
            "cwd": str(repo),
        })
        assert result.returncode == 0
        blob = json.loads(result.stdout)
        assert blob["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unknown_bash_command_is_a_noop(self, tmp_path):
        # `ls`, `grep`, etc. should not trigger projmem at all — no
        # point opening leases for read-only navigation.
        repo = self._setup_repo(tmp_path)
        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "cwd": str(repo),
        })
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_projmem_symbol_on_excluded_file_is_denied(self, tmp_path):
        # The user's reported case: they put an exclusion on the dir,
        # then the agent ran `projmem symbol app --file src/excluded/app.java`
        # — bypassed the hook entirely because that wasn't in the
        # original watch list. Now the hook recognises projmem
        # subcommands and extracts the --file argument.
        repo = self._setup_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "excluded").mkdir()
        (repo / "src" / "excluded" / "app.java").write_text(
            "public class App {}\n")
        rc = _run_cli(["index"], cwd=repo)
        assert rc.returncode == 0, rc.stderr
        _run_cli([
            "note", "add", "src/excluded/", "--kind", "exclude",
            "deprecated — do not read",
        ], cwd=repo)

        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "projmem symbol app --file src/excluded/app.java "
                    "--context 20"
                ),
            },
            "cwd": str(repo),
        })
        assert result.returncode == 0
        blob = json.loads(result.stdout)
        out = blob["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny", out
        assert "OUT OF SCOPE" in out["permissionDecisionReason"]
        assert "src/excluded/app.java" in out["permissionDecisionReason"]

    def test_projmem_introspection_subcommands_are_NOT_blocked(self, tmp_path):
        # `projmem context` IS how the hook discovers the exclusion —
        # blocking it would create a catch-22 where the agent can't
        # learn about the exclusion. Same for editing / creating /
        # deleting (those return the warning in their own response).
        repo = self._setup_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "excluded").mkdir()
        (repo / "src" / "excluded" / "x.py").write_text("x = 1\n")
        _run_cli(["index"], cwd=repo)
        _run_cli([
            "note", "add", "src/excluded/", "--kind", "exclude",
            "out of scope",
        ], cwd=repo)

        for subcmd in ("context", "editing", "notes"):
            result = self._invoke_hook(repo, {
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"projmem {subcmd} src/excluded/x.py",
                },
                "cwd": str(repo),
            })
            assert result.returncode == 0
            # Either empty output (no targets extracted) or context-only.
            if result.stdout.strip():
                blob = json.loads(result.stdout)
                out = blob.get("hookSpecificOutput") or {}
                assert out.get("permissionDecision") != "deny", (
                    f"projmem {subcmd} was wrongly blocked"
                )

    def test_cat_on_excluded_path_is_denied(self, tmp_path):
        # Same root cause covers every plain reader command — agents
        # love to `cat foo.py` to inspect content.
        repo = self._setup_repo(tmp_path)
        (repo / "secret.py").write_text("API_KEY = 'xxx'\n")
        _run_cli(["index"], cwd=repo)
        _run_cli([
            "note", "add", "secret.py", "--kind", "exclude",
            "credentials — never read",
        ], cwd=repo)

        for cmd in ("cat secret.py",
                     "head -n 5 secret.py",
                     "tail secret.py",
                     "less secret.py"):
            result = self._invoke_hook(repo, {
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
                "cwd": str(repo),
            })
            assert result.returncode == 0, (cmd, result.stderr)
            blob = json.loads(result.stdout)
            assert blob["hookSpecificOutput"]["permissionDecision"] == "deny", (
                f"`{cmd}` was not denied"
            )

    def test_grep_pattern_arg_is_not_treated_as_path(self, tmp_path):
        # `grep PATTERN excluded.py` — PATTERN must NOT trigger a
        # spurious lookup as if it were a path. Otherwise the agent
        # gets a false "PATTERN not found in index" error.
        repo = self._setup_repo(tmp_path)
        (repo / "secret.py").write_text("API_KEY = 'xxx'\n")
        _run_cli(["index"], cwd=repo)
        _run_cli([
            "note", "add", "secret.py", "--kind", "exclude",
            "credentials",
        ], cwd=repo)

        # Pattern that doesn't match a file — must not trigger deny by
        # itself. The excluded file does.
        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {"command": "grep MAGIC secret.py"},
            "cwd": str(repo),
        })
        assert result.returncode == 0
        blob = json.loads(result.stdout)
        assert blob["hookSpecificOutput"]["permissionDecision"] == "deny"
        # The pattern "MAGIC" must NOT appear in the deny reason as a
        # path — only secret.py should.
        assert "secret.py" in blob["hookSpecificOutput"]["permissionDecisionReason"]

    def test_projmem_at_with_line_citation_is_checked(self, tmp_path):
        # `projmem at src/excluded/app.java:5` carries a `:line`
        # suffix — the path extractor must strip the citation before
        # looking up the exclusion.
        repo = self._setup_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "excluded").mkdir()
        (repo / "src" / "excluded" / "app.java").write_text(
            "public class App {}\n")
        _run_cli(["index"], cwd=repo)
        _run_cli([
            "note", "add", "src/excluded/", "--kind", "exclude",
            "out of scope",
        ], cwd=repo)

        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {
                "command": "projmem at src/excluded/app.java:5",
            },
            "cwd": str(repo),
        })
        assert result.returncode == 0
        blob = json.loads(result.stdout)
        assert blob["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_cli_symbol_surfaces_exclusion_in_json_output(self, tmp_path):
        # Defense in depth: agents that DON'T run the Claude Code
        # PreToolUse hook (Codex, Gemini, plain API) still see the
        # OUT OF SCOPE message in the JSON output of read-style
        # commands. The hook BLOCKS; this WARNS.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "secret.py").write_text("def hello(): return 1\n")
        rc = _run_cli(["index"], cwd=repo)
        assert rc.returncode == 0, rc.stderr
        rc = _run_cli([
            "note", "add", "secret.py", "--kind", "exclude",
            "credentials — never read",
        ], cwd=repo)
        assert rc.returncode == 0, rc.stderr

        rc = _run_cli([
            "symbol", "hello", "--file", "secret.py", "--json",
        ], cwd=repo)
        assert rc.returncode == 0, rc.stderr
        out = json.loads(rc.stdout)
        warnings = out.get("exclusion_warnings") or []
        assert any("OUT OF SCOPE" in w for w in warnings), warnings
        assert any("secret.py" in w for w in warnings)

    def test_cli_at_surfaces_exclusion_in_json_output(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "secret.py").write_text("def hello(): return 1\n")
        _run_cli(["index"], cwd=repo)
        _run_cli([
            "note", "add", "secret.py", "--kind", "exclude",
            "credentials",
        ], cwd=repo)

        rc = _run_cli(["at", "secret.py:1", "--json"], cwd=repo)
        assert rc.returncode == 0, rc.stderr
        out = json.loads(rc.stdout)
        assert out.get("exclusion_warnings")

    def test_shell_injection_in_bash_command_is_inert(self, tmp_path):
        # Defense check — even if a malicious user crafts a bash
        # command with substitutions, shlex parses it as literal tokens.
        repo = self._setup_repo(tmp_path)
        canary = tmp_path / "canary.touched"
        result = self._invoke_hook(repo, {
            "tool_name": "Bash",
            "tool_input": {"command": f"rm $(touch {canary})"},
            "cwd": str(repo),
        })
        assert result.returncode == 0
        assert not canary.exists(), (
            "shell expansion ran inside the hook — critical CVE"
        )
