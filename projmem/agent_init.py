"""projmem/agent_init.py — write agent-instruction templates into a repo.

Used by `projmem init`. Drops one or more agent-specific files so any
LLM tool that reads a known instruction format (Claude Code, Codex,
OpenCode, Cursor, Gemini CLI, Kiro, Aider, Antigravity, VS Code Copilot
Chat, etc.) automatically learns the projmem workflow on first session.

Each platform entry can drop multiple files (e.g., a markdown doc PLUS
a settings.json hook). Hook files are written via deep-merge so we
don't trample the user's existing settings.

Safe by default: never overwrites existing markdown files unless
--force; settings.json files are merged additively. Returns a structured
result so the CLI can JSON-dump it.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# Each entry in TEMPLATES describes what to drop for one platform.
# Schema:
#   purpose : str — one-line purpose
#   files   : list[FileSpec]
#     - dest   : relative path under repo root
#     - source : relative path under projmem/templates/
#     - merge  : "text" | "json"  (default "text")
#                "json" deep-merges with an existing file at dest (so we
#                don't blow away a user's other settings).
TEMPLATES: Dict[str, Dict[str, Any]] = {
    "agents": {
        "purpose": "Generic AGENTS.md — read by Codex, Aider, Droid, "
                   "Trae, Hermes, OpenClaw and most other CLI agents.",
        "files": [
            {"dest": "AGENTS.md",
             "source": "AGENTS.md",
             "merge": "text"},
        ],
    },
    "claude": {
        "purpose": "Anthropic Claude Code: CLAUDE.md + PreToolUse hook "
                   "in .claude/settings.json.",
        "files": [
            {"dest": "CLAUDE.md",
             "source": "CLAUDE.md",
             "merge": "text"},
            {"dest": ".claude/settings.json",
             "source": "claude/settings.json",
             "merge": "json"},
        ],
    },
    "codex": {
        "purpose": "OpenAI Codex CLI: AGENTS.md + PreToolUse hook in "
                   ".codex/hooks.json.",
        "files": [
            {"dest": "AGENTS.md",
             "source": "AGENTS.md",
             "merge": "text"},
            {"dest": ".codex/hooks.json",
             "source": "codex/hooks.json",
             "merge": "json"},
        ],
    },
    "cursor": {
        "purpose": "Cursor IDE: alwaysApply rule at "
                   ".cursor/rules/projmem.mdc (modern) AND legacy "
                   ".cursorrules.",
        "files": [
            {"dest": ".cursor/rules/projmem.mdc",
             "source": "cursor/projmem.mdc",
             "merge": "text"},
            {"dest": ".cursorrules",
             "source": ".cursorrules",
             "merge": "text"},
        ],
    },
    "opencode": {
        "purpose": "OpenCode: AGENTS.md + tool.execute.before plugin "
                   "at .opencode/plugins/projmem.js.",
        "files": [
            {"dest": "AGENTS.md",
             "source": "AGENTS.md",
             "merge": "text"},
            {"dest": ".opencode/plugins/projmem.js",
             "source": "opencode/projmem.js",
             "merge": "text"},
        ],
    },
    "gemini": {
        "purpose": "Google Gemini CLI: GEMINI.md + BeforeTool hook in "
                   ".gemini/settings.json.",
        "files": [
            {"dest": "GEMINI.md",
             "source": "GEMINI.md",
             "merge": "text"},
            {"dest": ".gemini/settings.json",
             "source": "gemini/settings.json",
             "merge": "json"},
        ],
    },
    "kiro": {
        "purpose": "Kiro: always-included steering doc at "
                   ".kiro/steering/projmem.md.",
        "files": [
            {"dest": ".kiro/steering/projmem.md",
             "source": "kiro/projmem.md",
             "merge": "text"},
        ],
    },
    "antigravity": {
        "purpose": "Antigravity: rules + workflows under .agent/.",
        "files": [
            {"dest": ".agent/rules/projmem.md",
             "source": "antigravity/projmem.md",
             "merge": "text"},
            {"dest": ".agent/workflows/projmem.md",
             "source": "antigravity/workflow.md",
             "merge": "text"},
        ],
    },
    "copilot": {
        "purpose": "VS Code Copilot Chat: instructions at "
                   ".github/copilot-instructions.md.",
        "files": [
            {"dest": ".github/copilot-instructions.md",
             "source": "github/copilot-instructions.md",
             "merge": "text"},
        ],
    },
    # Aliases / "AGENTS.md only" agents — these bare-string entries
    # resolve to the `agents` set so users get a familiar config name.
    # Listed in CHOICES for discoverability.
}

# Aliases that resolve to the "agents" template (AGENTS.md-only).
AGENTS_ALIASES = ("aider", "droid", "trae", "hermes", "openclaw")


# Lookup CHOICES exposes every accepted value (for argparse `choices=`).
CHOICES = (["auto", "all"] + sorted(TEMPLATES.keys())
           + sorted(AGENTS_ALIASES))


def _templates_dir() -> Path:
    return Path(__file__).parent / "templates"


def write_templates(repo_root: str, template: str = "agents",
                    force: bool = False) -> Dict[str, Any]:
    """Write the requested template(s) into `repo_root`.

    `template` can be any key in TEMPLATES, an alias in AGENTS_ALIASES,
    or "all". Returns:

      {
        wrote:   [absolute paths written],
        merged:  [absolute paths merged into existing JSON],
        skipped: [{path, reason}, ...],
        errors:  [{path, error}, ...],
        template: <name>,
      }
    """
    src_dir = _templates_dir()
    if not src_dir.is_dir():
        return {
            "wrote": [], "merged": [], "skipped": [], "errors": [
                {"path": str(src_dir),
                 "error": "templates directory missing from package"}],
            "template": template,
        }

    # Resolve template name to one or more entry dicts.
    entries: List[Dict[str, Any]] = []
    if template == "all":
        entries = list(TEMPLATES.values())
        chosen_name = "all"
    elif template in TEMPLATES:
        entries = [TEMPLATES[template]]
        chosen_name = template
    elif template in AGENTS_ALIASES:
        entries = [TEMPLATES["agents"]]
        chosen_name = template
    else:
        return {
            "wrote": [], "merged": [], "skipped": [], "errors": [
                {"path": template,
                 "error": f"unknown template {template!r}; "
                          f"choices: {CHOICES}"}],
            "template": template,
        }

    repo_path = Path(repo_root)
    wrote: List[str] = []
    merged: List[str] = []
    skipped: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []

    for entry in entries:
        for fs in entry.get("files", []):
            src_file = src_dir / fs["source"]
            dest = repo_path / fs["dest"]
            mode = fs.get("merge", "text")
            if not src_file.is_file():
                errors.append({"path": str(src_file),
                               "error": "source template missing"})
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if mode == "json":
                    result = _write_json_merged(src_file, dest, force=force)
                    if result == "wrote":
                        wrote.append(str(dest))
                    elif result == "merged":
                        merged.append(str(dest))
                    else:
                        skipped.append({
                            "path": str(dest),
                            "reason": result,
                        })
                else:
                    if dest.exists() and not force:
                        skipped.append({
                            "path":   str(dest),
                            "reason": "exists; pass --force to overwrite",
                        })
                        continue
                    content = src_file.read_text(encoding="utf-8")
                    dest.write_text(content, encoding="utf-8")
                    wrote.append(str(dest))
            except OSError as e:
                errors.append({"path": str(dest), "error": str(e)})

    return {
        "wrote":    wrote,
        "merged":   merged,
        "skipped":  skipped,
        "errors":   errors,
        "template": chosen_name,
        "next_step": (
            "Your LLM agent should now read the new file(s) at session "
            "start. Verify with: `projmem usage` to see the agent-facing "
            "command catalog."
        ),
    }


def _write_json_merged(src_file: Path, dest: Path, *,
                       force: bool) -> str:
    """Write a JSON template file, deep-merging with whatever is already
    at `dest`. Lists with the same key concatenate (deduped). Returns
    one of: "wrote", "merged", "<reason>" (skip).
    """
    try:
        new_data = json.loads(src_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"source not valid JSON: {e}"
    if dest.exists() and not force:
        try:
            cur_data = json.loads(dest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # User has a non-JSON or corrupt file at dest; refuse to
            # touch it without --force so we don't destroy their work.
            return ("existing file is not valid JSON; pass --force to "
                    "replace")
        # Detect: already merged? If our marker key is present we treat
        # the file as up-to-date.
        merged = _deep_merge(cur_data, new_data)
        if merged == cur_data:
            return "already includes projmem block"
        dest.write_text(json.dumps(merged, indent=2) + "\n",
                        encoding="utf-8")
        return "merged"
    dest.write_text(json.dumps(new_data, indent=2) + "\n",
                    encoding="utf-8")
    return "wrote"


def _deep_merge(base: Any, overlay: Any) -> Any:
    """Conservative merge: dicts merge key-by-key; lists concat-dedupe;
    scalars from `overlay` win."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            if k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    if isinstance(base, list) and isinstance(overlay, list):
        out = list(base)
        for item in overlay:
            if item not in out:
                out.append(item)
        return out
    return overlay
