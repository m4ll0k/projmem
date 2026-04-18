"""projmem/hooks.py — install/uninstall git hooks that surface drift.

Drops two hooks into `.git/hooks/`:

  post-commit   → reindex (forced) then revalidate notes touching files
                  changed in the just-made commit. Surfaces REFUTED
                  claims the second they're introduced.
  post-checkout → same behavior on branch switches and checkouts that
                  cross commits, so the agent never operates against
                  notes verified on a different branch.

Idempotent:
  * Each hook is wrapped in BEGIN/END markers so we can detect, update,
    and remove our block without trampling user customizations.
  * If the hook file already exists from another tool, we APPEND our
    block (and remove only that block on `uninstall`).
  * Existing executable bit is preserved; we set +x when we create
    a fresh file.
"""
from __future__ import annotations
import os
import stat
from typing import Any, Dict, List, Tuple

BEGIN_MARKER = "# >>> projmem hook (managed) >>>"
END_MARKER   = "# <<< projmem hook (managed) <<<"

# Each hook gets the same body — small, fast, and tolerant of missing
# projmem (the `command -v` check) so we don't break a checkout in
# environments where projmem isn't on PATH. We call `projmem notes`
# (not `note-verify`) because notes revalidates EVERY annotation in
# one pass and reports `contradicted` count in the JSON header — no
# per-target loop needed.
HOOK_BODY_TEMPLATE = """\
{begin}
# Auto-installed by `projmem hook install`. Edit by re-running install,
# uninstall by `projmem hook uninstall` (preserves the rest of this file).
if command -v projmem >/dev/null 2>&1; then
    projmem index --force >/dev/null 2>&1 || true
    refuted=$(projmem notes --json 2>/dev/null \\
              | python3 -c 'import sys,json
d=json.load(sys.stdin)
print(len(d.get("contradicted",[])))' 2>/dev/null || echo 0)
    if [ "${{refuted:-0}}" != "0" ]; then
        echo ""
        echo "projmem: ${{refuted}} note(s) contradicted after {hook_name}."
        echo "  -> projmem notes              # see which beliefs are wrong"
        echo "  -> projmem report             # full digest in REPORT.md"
        echo ""
    fi
fi
{end}
"""

HOOKS = ("post-commit", "post-checkout")


def install(repo_root: str, *, force: bool = False) -> Dict[str, Any]:
    """Install our managed hook block into the standard hook files.
    `force=True` overwrites the entire hook file (use sparingly — wipes
    other tools' hooks); default behavior appends/updates our block.
    """
    git_dir, err = _resolve_git_dir(repo_root)
    if err:
        # Round-5-r2 F010 (refined in r3): standardized envelope —
        # `message` MUST be a sentence string, not a dict. The
        # resolver returns `{reason: <sentence>}`; unwrap so the
        # caller doesn't see `message: {reason: ...}` shape drift.
        return {
            "error":   "no-git-repo",
            "message": err.get("reason") if isinstance(err, dict) else str(err),
            "hint": ("Hooks live under `.git/hooks`. Run "
                      "`git init` first, or pass `--path <git-root>` "
                      "if you're invoking from a worktree."),
        }
    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    installed: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    for hook_name in HOOKS:
        path = os.path.join(hooks_dir, hook_name)
        body = HOOK_BODY_TEMPLATE.format(
            begin=BEGIN_MARKER, end=END_MARKER, hook_name=hook_name)
        try:
            new_content = _merge_hook(path, body, force=force)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            _ensure_executable(path)
            installed.append({"hook": hook_name, "path": path})
        except OSError as e:
            errors.append({"hook": hook_name, "path": path, "error": str(e)})
    return {
        "installed": installed,
        "errors":    errors,
        "next_step": ("Make a commit or checkout — the hook will run "
                      "`projmem index --force` then revalidate notes "
                      "touching the changed files. REFUTED count > 0 "
                      "prints a one-line warning."),
    }


def uninstall(repo_root: str) -> Dict[str, Any]:
    """Remove only our managed block from each hook file. If a hook
    file becomes empty (only had our block), delete it entirely."""
    git_dir, err = _resolve_git_dir(repo_root)
    if err:
        return {
            "error":   "no-git-repo",
            "message": err,
            "hint": ("Hooks live under `.git/hooks`. Run "
                      "`git init` first, or pass `--path <git-root>`."),
        }
    hooks_dir = os.path.join(git_dir, "hooks")
    removed: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    for hook_name in HOOKS:
        path = os.path.join(hooks_dir, hook_name)
        if not os.path.isfile(path):
            skipped.append({"hook": hook_name, "reason": "no hook file"})
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cur = f.read()
        except OSError as e:
            errors.append({"hook": hook_name, "error": str(e)})
            continue
        new_content = _strip_block(cur).rstrip()
        if new_content == cur.rstrip():
            skipped.append({"hook": hook_name,
                            "reason": "no projmem block to remove"})
            continue
        if new_content.strip() in ("", "#!/bin/sh", "#!/usr/bin/env bash"):
            try:
                os.unlink(path)
                removed.append({"hook": hook_name, "path": path,
                                 "deleted": True})
            except OSError as e:
                errors.append({"hook": hook_name, "error": str(e)})
        else:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content + "\n")
                removed.append({"hook": hook_name, "path": path,
                                 "deleted": False})
            except OSError as e:
                errors.append({"hook": hook_name, "error": str(e)})
    return {"removed": removed, "skipped": skipped, "errors": errors}


def status(repo_root: str) -> Dict[str, Any]:
    """Report what's currently installed without modifying anything."""
    git_dir, err = _resolve_git_dir(repo_root)
    if err:
        return {
            "error":   "no-git-repo",
            "message": err.get("reason") if isinstance(err, dict) else str(err),
            "git_dir": None,
            "hooks":   [],
            "hint": ("Hooks live under `.git/hooks`. Run "
                      "`git init` first, or pass `--path <git-root>`."),
        }
    hooks_dir = os.path.join(git_dir, "hooks")
    out: List[Dict[str, Any]] = []
    for hook_name in HOOKS:
        path = os.path.join(hooks_dir, hook_name)
        entry: Dict[str, Any] = {"hook": hook_name, "path": path,
                                 "exists": os.path.isfile(path),
                                 "managed": False}
        if entry["exists"]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    txt = f.read()
                entry["managed"] = BEGIN_MARKER in txt
            except OSError:
                entry["managed"] = False
        out.append(entry)
    return {"git_dir": git_dir, "hooks": out}


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

def _resolve_git_dir(repo_root: str) -> Tuple[str, Dict[str, str] | None]:
    """Locate the git directory (handles worktrees via `.git` files).
    Returns (git_dir_abs, error_dict) — error is None on success."""
    cand = os.path.join(repo_root, ".git")
    if os.path.isdir(cand):
        return cand, None
    if os.path.isfile(cand):
        # Worktree: `.git` is a file pointing at gitdir.
        try:
            with open(cand, "r", encoding="utf-8") as f:
                txt = f.read().strip()
        except OSError as e:
            return "", {"reason": f"cannot read .git pointer: {e}"}
        if txt.startswith("gitdir:"):
            ref = txt.split(":", 1)[1].strip()
            if not os.path.isabs(ref):
                ref = os.path.join(repo_root, ref)
            if os.path.isdir(ref):
                return ref, None
            return "", {"reason": f"gitdir target missing: {ref}"}
        return "", {"reason": f"unrecognized .git contents: {txt[:80]!r}"}
    return "", {"reason": "not a git repository (.git not found at "
                          f"{cand})"}


def _merge_hook(path: str, our_block: str, *, force: bool) -> str:
    """Compose the new hook file content. Cases:

      - file missing      → write shebang + block
      - file present, no marker → append block (with separator newline)
      - file present, has marker → replace existing block in-place
      - force=True        → ignore everything; write shebang + block
    """
    if force or not os.path.isfile(path):
        return "#!/bin/sh\n" + our_block + "\n"
    try:
        with open(path, "r", encoding="utf-8") as f:
            cur = f.read()
    except OSError:
        return "#!/bin/sh\n" + our_block + "\n"
    if BEGIN_MARKER in cur and END_MARKER in cur:
        # Replace the existing managed block.
        before, _, rest = cur.partition(BEGIN_MARKER)
        _, _, after = rest.partition(END_MARKER)
        return (before.rstrip() + "\n" + our_block.rstrip() + "\n"
                + after.lstrip()).rstrip() + "\n"
    # No marker — append.
    sep = "" if cur.endswith("\n") else "\n"
    return cur + sep + "\n" + our_block.rstrip() + "\n"


def _strip_block(content: str) -> str:
    """Remove our managed block from `content`. Tolerates missing markers
    (returns content unchanged)."""
    if BEGIN_MARKER not in content or END_MARKER not in content:
        return content
    before, _, rest = content.partition(BEGIN_MARKER)
    _, _, after = rest.partition(END_MARKER)
    return (before.rstrip() + "\n" + after.lstrip())


def _ensure_executable(path: str) -> None:
    """`chmod +x` on the hook so git will actually run it."""
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
