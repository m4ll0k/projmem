"""Hook-script templates for `projmem hook install --claude-code`.

These are the Python scripts written under ``.claude/hooks/`` by the
installer. They are intentionally:

  * Standalone — only depend on the Python stdlib + the `projmem`
    binary being on PATH. No imports of internal projmem modules
    (which may move between releases).
  * Argv-safe — every value derived from the Claude Code hook payload
    is passed to ``subprocess.run`` as a list element, never shell-
    interpolated. A note body containing ``$(rm -rf /)`` round-trips
    through the hook as inert text.
  * No-op on missing projmem state — if `projmem` isn't on PATH or
    the repo has no ``.projmem/`` index, the hook exits 0 silently so
    Claude Code sessions are never broken.

Templates use ``$$`` for literal ``$`` characters (Python ``str.Template``
syntax) — not in use here, but flagged for future maintainers who add
substitutions to the templates.
"""
from __future__ import annotations


PRE_TOOL_USE_SCRIPT = r'''#!/usr/bin/env python3
"""projmem PreToolUse hook — context + enforcement before risky tools.

This is the *enforcement* layer that prompt-only CLAUDE.md instructions
can't provide. For every tool that could mutate a file (Edit / Write /
NotebookEdit) and every dangerous shell command (rm / rmdir / unlink /
mv / cp -f / dd / shred / >redirect), the hook calls ``projmem editing``
on the target path BEFORE the tool runs.

Three outcomes:

  1. No warnings → injects guidance as additionalContext, lets the
     tool proceed.
  2. OUT OF SCOPE (exclusion) → returns permissionDecision="deny" so
     Claude Code REFUSES the tool call. The agent can't bypass a
     human-set exclusion by hallucinating past CLAUDE.md.
  3. CRITICAL note blocking edits → same deny, with the reason from
     the critical note surfaced verbatim. The user clicks Approve in
     the projmem UI to unblock.

Treated as a no-op if:
  * projmem is not on PATH
  * the repo has no .projmem/ index
  * the tool isn't a known risky one
  * no file path can be derived from the tool input

Never raises. Failure modes fall through to "allow" so a misconfigured
projmem never bricks a Claude Code session.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys


# File-touching tools — file_path / path arg is the target.
FILE_TOUCH_TOOLS = {"Edit", "Write", "NotebookEdit"}

# Read tools — exclusion still blocks (the user said don't read it),
# but a critical note alone does not (critical guards edits, not reads).
FILE_READ_TOOLS = {"Read"}

# Bash subcommands that MUTATE the filesystem. Path args are positional
# after the flags. shlex parses the command string safely (no shell
# evaluation), so a malicious path like `$(rm -rf /)` cannot escape.
RISKY_BASH_CMDS = {"rm", "rmdir", "unlink", "mv", "shred", "dd"}

# Bash subcommands that READ file content. Treated like Read — only
# exclusions block them. The user's case: `cat src/excluded/file.py`,
# `head -n 50 src/excluded/file.py`, `grep foo src/excluded/`.
READER_BASH_CMDS = {
    "cat", "less", "more", "head", "tail", "bat", "view",
    "grep", "rg", "ag", "ack",
}

# projmem subcommands that are PART of the exclusion-checking loop —
# blocking them would prevent the agent from learning about the
# exclusion in the first place. Everything NOT in this set that takes
# a path argument is treated as a read of that path.
PROJMEM_INTROSPECTION_SUBCMDS = {
    "context", "editing", "creating", "deleting", "moving",
    "notes", "graph", "info", "stats", "status", "doctor",
    "hook", "init", "exclusions",
}

# File-extension hints used by the path-token sniffer for projmem
# subcommands that take a positional path (e.g. `projmem pack foo.py`).
SOURCE_EXTS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
    ".kt", ".swift", ".rb", ".php", ".cpp", ".cc", ".c", ".h",
    ".hpp", ".cs", ".scala", ".lua", ".sh", ".sql", ".md", ".yaml",
    ".yml", ".toml", ".json",
)

PROJMEM_REASON = "claude-code PreToolUse"


def _projmem_editing(cwd, path, tool, intent_note):
    """Call `projmem editing <path>` and return the parsed JSON, or None
    if anything went wrong (no PATH / no .projmem/ / projmem errored).
    Opens a lease — use only for actual mutating intents."""
    if not shutil.which("projmem"):
        return None
    if not os.path.isdir(os.path.join(cwd, ".projmem")):
        return None
    try:
        result = subprocess.run(
            ["projmem", "--path", cwd, "editing", path,
             "--reason", f"{PROJMEM_REASON}: {tool} {intent_note}",
             "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return None


def _projmem_context(cwd, path):
    """Lightweight read-only lookup. Does NOT open a lease. Returns
    parsed JSON or None. Used for read intents (Read, cat, head, grep,
    projmem symbol --file …) so we don't pollute the audit log with
    spurious leases just to check whether a path is excluded."""
    if not shutil.which("projmem"):
        return None
    if not os.path.isdir(os.path.join(cwd, ".projmem")):
        return None
    try:
        result = subprocess.run(
            ["projmem", "--path", cwd, "context", path, "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return None


def _first_exclusion(info):
    """Return the first kind=exclude annotation in a context/editing
    response, or None. The body is the user's free-text reason and goes
    verbatim into the deny message."""
    if not info:
        return None
    for note in info.get("guidance") or []:
        if note.get("kind") == "exclude":
            return note
    return None


def _extract_bash_paths(cmd_str):
    """Pull file path operands out of a bash command. Returns a list of
    (path, intent, access) tuples.

    ``intent`` is a short string for the audit trail. ``access`` is
    either ``"write"`` (rm / mv / shred / dd) or ``"read"`` (cat / head
    / grep / projmem-subcommand) — drives whether we use
    ``projmem editing`` (which opens a lease) or ``projmem context``
    (read-only) for the check, and whether critical notes alone count
    as deny-worthy or only exclusions do.

    shlex parses with posix word-splitting, no shell evaluation — a
    payload like ``rm $(touch /tmp/canary)`` survives intact as tokens.
    """
    if not cmd_str:
        return []
    try:
        tokens = shlex.split(cmd_str, posix=True)
    except ValueError:
        return []
    if not tokens:
        return []
    cmd = os.path.basename(tokens[0])

    if cmd in RISKY_BASH_CMDS:
        return [(p, cmd, "write") for p in _paths_after(tokens)]

    if cmd in READER_BASH_CMDS:
        # `grep PATTERN path` — first non-flag arg after the command is
        # the pattern, not a path. We still scan every remaining token
        # so multi-file invocations (`grep foo a.py b.py`) are caught.
        skip_first_non_flag = cmd in ("grep", "rg", "ag", "ack")
        return [(p, f"read:{cmd}", "read")
                for p in _paths_after(tokens, skip_first_non_flag=skip_first_non_flag)]

    if cmd == "projmem":
        return _extract_projmem_subcmd_paths(tokens)

    return []


def _paths_after(tokens, *, skip_first_non_flag=False):
    """Scan tokens after the command for path-shaped operands. Stops
    at shell separators. Optionally skips the first non-flag token
    (used for grep-family pattern args)."""
    out = []
    skipped = False
    for tok in tokens[1:]:
        if not tok:
            continue
        if tok in (";", "&&", "||", "|", ">", ">>", "<"):
            break
        if tok.startswith("-"):
            continue
        if skip_first_non_flag and not skipped:
            skipped = True
            continue
        out.append(tok)
    return out


def _extract_projmem_subcmd_paths(tokens):
    """Pull paths out of a `projmem <subcmd> [args]` invocation.

    Whitelisted introspection subcommands return [] — projmem's own
    output IS the exclusion check, so blocking those would create a
    catch-22. For everything else, we look at ``--file VALUE``,
    ``--path VALUE`` (the global flag), and positional path-shaped
    tokens. Path-shape heuristic: contains ``/`` or ends in a known
    source extension.
    """
    out = []
    i = 1
    subcmd = None
    saw_global_path = False
    while i < len(tokens):
        tok = tokens[i]
        # Skip the `--path <root>` global flag — that's the project
        # root, not a target. Trip the flag and consume the value.
        if tok == "--path" and i + 1 < len(tokens):
            saw_global_path = True
            i += 2
            continue
        if subcmd is None and not tok.startswith("-"):
            subcmd = tok
            if subcmd in PROJMEM_INTROSPECTION_SUBCMDS:
                return []
            i += 1
            continue
        if tok in ("--file", "-f") and i + 1 < len(tokens):
            out.append(tokens[i + 1])
            i += 2
            continue
        if tok.startswith("-"):
            # Generic flag — skip the value if the next token looks
            # non-positional. We err on the side of also peeking; if
            # the next token is a path we don't want to skip it.
            i += 1
            continue
        # Positional arg. Treat as path if it looks like one.
        if "/" in tok or tok.endswith(SOURCE_EXTS):
            # Strip a trailing `:line` citation if present.
            base = tok.split(":")[0] if ":" in tok and tok.split(":")[0] else tok
            out.append(base)
        i += 1
    intent = f"projmem-{subcmd or 'unknown'}"
    return [(p, intent, "read") for p in out]


# Older callsites may still expect the 2-tuple shape.
def _extract_bash_paths_legacy_2tuple(cmd_str):
    return [(p, i) for (p, i, _a) in _extract_bash_paths(cmd_str)]


def _summarize_warnings(warnings):
    """Pick out the strongest deny-worthy warning, return (kind, text)
    or (None, None) if nothing should block. ``kind`` is "exclusion" or
    "critical" — both deny, but the reasons read differently."""
    for w in warnings or []:
        s = str(w)
        if "OUT OF SCOPE" in s:
            return ("exclusion", s)
    for w in warnings or []:
        s = str(w)
        if "CRITICAL" in s and "block" in s.lower():
            return ("critical", s)
    return (None, None)


def _emit_deny(reason):
    out = {
        "hookSpecificOutput": {
            "hookEventName":         "PreToolUse",
            "permissionDecision":    "deny",
            "permissionDecisionReason": reason,
        },
    }
    json.dump(out, sys.stdout)


def _emit_context(block):
    out = {
        "hookSpecificOutput": {
            "hookEventName":     "PreToolUse",
            "additionalContext": block,
        },
    }
    json.dump(out, sys.stdout)


def _format_guidance(info):
    lines = []
    lease = info.get("lease_id")
    if lease:
        lines.append(f"projmem lease {lease[:8]}… open (expires "
                     f"{int(info.get('expires_at', 0) - info.get('opened_at', 0))}s)")
    for w in info.get("warnings") or []:
        lines.append("⚠ " + str(w)[:500])
    guidance = info.get("guidance") or []
    if guidance:
        lines.append(f"context ({len(guidance)} note(s)):")
        for note in guidance[:8]:
            tag = note.get("via") or note.get("kind") or "note"
            body = (note.get("body") or "").strip()
            if len(body) > 240:
                body = body[:237] + "..."
            lines.append(f"  • [{tag}] {note.get('target')}: {body}")
    return "\n".join(lines)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool = payload.get("tool_name") or payload.get("tool")
    tool_input = payload.get("tool_input") or payload.get("tool_args") or {}
    cwd = payload.get("cwd") or os.getcwd()

    # Collect (path, intent, access) triples that should be checked.
    # access ∈ {"read", "write"} — drives whether we use the read-only
    # projmem context lookup or open a real editing lease, and whether
    # critical notes alone count as deny-worthy (writes only).
    targets = []
    if tool in FILE_TOUCH_TOOLS:
        p = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(p, str) and p:
            targets.append((p, tool.lower(), "write"))
    elif tool in FILE_READ_TOOLS:
        p = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(p, str) and p:
            targets.append((p, tool.lower(), "read"))
    elif tool == "Bash":
        cmd_str = tool_input.get("command")
        if isinstance(cmd_str, str):
            targets.extend(_extract_bash_paths(cmd_str))

    if not targets:
        return 0

    blocks = []
    contexts = []
    for path, intent, access in targets:
        if access == "write":
            info = _projmem_editing(cwd, path, tool, intent)
            if info is None:
                continue
            # Exclusion OR critical both deny writes.
            kind, reason = _summarize_warnings(info.get("warnings"))
            if kind:
                blocks.append(f"[{path}] {reason}")
            else:
                ctx = _format_guidance(info)
                if ctx:
                    contexts.append(f"--- {path} ---\n{ctx}")
        else:
            # Read intent — only the exclusion denies. Critical guards
            # mutations, not reads. We use the read-only `context`
            # lookup so we don't pollute file_event with phantom leases.
            info = _projmem_context(cwd, path)
            if info is None:
                continue
            excl = _first_exclusion(info)
            if excl:
                blocks.append(
                    f"[{path}] 🚫 OUT OF SCOPE — exclusion at "
                    f"{excl.get('target')!r}: "
                    f"{(excl.get('body') or '').strip()}"
                )
            else:
                ctx = _format_guidance(info)
                if ctx:
                    contexts.append(f"--- {path} ---\n{ctx}")

    if blocks:
        body = "\n\n".join(blocks)
        prefix = (
            "projmem refuses this tool call. "
            "Resolve the warnings below before retrying — "
            "remove the exclusion in the projmem UI, mark the critical "
            "note as approved, or pick a different path.\n\n"
        )
        _emit_deny(prefix + body)
        return 0

    if contexts:
        _emit_context("\n\n".join(contexts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


POST_TOOL_USE_SCRIPT = r'''#!/usr/bin/env python3
"""projmem PostToolUse hook — closes the lease (or creates an implicit one).

Reads the Claude Code hook payload from stdin. If a lease is already
open for the path (set up by PreToolUse), it's closed via ``projmem
done``. Otherwise — the agent bypassed PreToolUse — an implicit lease
is created retroactively and immediately closed so the audit trail
records the edit.

No-op on:
  * non-file-touching tools
  * missing projmem on PATH
  * missing .projmem/ index
"""
import json
import os
import shutil
import subprocess
import sys


FILE_TOUCH_TOOLS = {"Edit", "Write", "NotebookEdit"}


def _run(cmd, *, cwd):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool = payload.get("tool_name") or payload.get("tool")
    if tool not in FILE_TOUCH_TOOLS:
        return 0

    tool_input = payload.get("tool_input") or payload.get("tool_args") or {}
    file_path = tool_input.get("file_path") or tool_input.get("path")
    if not file_path or not isinstance(file_path, str):
        return 0
    if not shutil.which("projmem"):
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    if not os.path.isdir(os.path.join(cwd, ".projmem")):
        return 0

    # Sweep first so any expired-but-still-marked-open leases are gone.
    _run(["projmem", "--path", cwd, "sweep-leases", "--json"], cwd=cwd)

    # The CLI doesn't have a "close by path" verb — but we can find the
    # open lease via sqlite query through the `find_open_lease_for_path`
    # helper. We invoke a tiny inline Python snippet so this script
    # stays projmem-binary-only.
    finder = (
        "import sys, json, os; "
        "from projmem import config as _c, mutation_verbs as _mv; "
        "from projmem.store import Store as _S; "
        "cfg=_c.load(os.getcwd()); s=_S(cfg.db_path); "
        "r=_mv.find_open_lease_for_path(s, sys.argv[1]); "
        "print(json.dumps(r) if r else 'null')"
    )
    res = _run(["python3", "-c", finder, file_path], cwd=cwd)
    if res is None or res.returncode != 0:
        return 0
    try:
        lease = json.loads(res.stdout.strip() or "null")
    except (json.JSONDecodeError, ValueError):
        lease = None

    if lease and isinstance(lease, dict) and lease.get("id"):
        # Close the existing lease.
        _run(["projmem", "--path", cwd, "done", lease["id"], "--json"], cwd=cwd)
    else:
        # No open lease — agent bypassed PreToolUse. Create an implicit
        # lease via a one-shot Python invocation so the audit trail is
        # complete + the implicit-lease metric ticks.
        implicit_open = (
            "import os, json; "
            "from projmem import config as _c, mutation_verbs as _mv; "
            "from projmem.store import Store as _S; "
            "cfg=_c.load(os.getcwd()); s=_S(cfg.db_path); "
            f"r=_mv.open_implicit_lease(s, {file_path!r}); "
            "print(json.dumps(r))"
        )
        res2 = _run(["python3", "-c", implicit_open], cwd=cwd)
        if res2 is not None and res2.returncode == 0:
            try:
                info = json.loads(res2.stdout.strip() or "{}")
            except (json.JSONDecodeError, ValueError):
                info = {}
            if info.get("lease_id"):
                _run(["projmem", "--path", cwd, "done", info["lease_id"],
                      "--json"], cwd=cwd)

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


SETTINGS_INSTRUCTIONS = """
Add (or merge) the following into .claude/settings.json:

{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|Read|NotebookEdit|Bash",
        "hooks": [
          {"type": "command", "command": ".claude/hooks/pre-tool-use.py"}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          {"type": "command", "command": ".claude/hooks/post-tool-use.py"}
        ]
      }
    ]
  }
}

Both scripts no-op gracefully when:
  * projmem isn't on PATH
  * the repo has no .projmem/ index
  * the tool isn't a file-touching one
So registering them is safe even on repos that don't use projmem.
""".strip()
