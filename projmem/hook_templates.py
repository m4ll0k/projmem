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
"""projmem PreToolUse hook — injects context before file-touching tools.

Reads the Claude Code hook payload from stdin, calls ``projmem editing``
on the target path, and returns the formatted guidance via the
``additionalContext`` field so Claude sees it in the tool result.

Treated as a no-op if:
  * projmem is not on PATH
  * the repo has no .projmem/ index
  * the tool isn't a file-touching tool (Edit / Write / Read)
  * the tool input has no `file_path`

Never raises. Always exits 0 so a misconfigured projmem never breaks
a Claude Code session.
"""
import json
import os
import shutil
import subprocess
import sys


FILE_TOUCH_TOOLS = {"Edit", "Write", "Read", "NotebookEdit"}
PROJMEM_REASON = "claude-code PreToolUse"


def _silent_exit():
    sys.exit(0)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return _silent_exit() or 0

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

    # `editing` is double-duty: returns lease + guidance + history + warnings.
    # Argv only — file_path lands as a positional, reason as a flag value.
    # A path containing $(rm -rf /) survives intact.
    try:
        result = subprocess.run(
            ["projmem", "--path", cwd, "editing", file_path,
             "--reason", f"{PROJMEM_REASON}: {tool} on {file_path}",
             "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if result.returncode != 0:
        # Reason-quality or no-index — fail silent so the agent's flow
        # isn't blocked. We deliberately do NOT surface the projmem
        # error here because Claude Code would treat it as the tool's
        # error and the user would think Edit/Write failed.
        return 0

    try:
        info = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0

    # Build a human-readable context block. Note bodies are passed as
    # data only — no eval, no f-string injection into shell strings.
    lines = []
    lease = info.get("lease_id")
    if lease:
        lines.append(f"projmem lease {lease[:8]}… open (expires "
                     f"{int(info.get('expires_at', 0) - info.get('opened_at', 0))}s)")
    for w in info.get("warnings") or []:
        # Cap each warning at 500 chars to keep tool result lean.
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

    if not lines:
        return 0

    block = "\n".join(lines)
    out = {
        "hookSpecificOutput": {
            "hookEventName":     "PreToolUse",
            "additionalContext": block,
        },
    }
    json.dump(out, sys.stdout)
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
        "matcher": "Edit|Write|Read|NotebookEdit",
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
