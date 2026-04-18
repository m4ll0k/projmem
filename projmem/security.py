"""projmem/security.py — input sanitization in one place.

Consolidates the small but consequential helpers that were spread
across `symbols.py`, `packs.py`, `ask.py`, etc. Centralizing means a
single audit surface and one place to harden.

What lives here:

  Path / target normalization:
    - normalize_target_path(s)  : canonical form for a CLI-supplied path
    - looks_like_path(s)        : discriminator for path vs bare symbol
    - safe_filename(s)          : derive a filesystem-safe basename
                                  (used when persisting packs / reports)

  Body text:
    - sanitize_note_body(s, max_len)
        : strip control characters, normalize line endings, hard-cap
          length. Used when persisting agent-written notes.

  Symbol / command cleansing:
    - sanitize_symbol_name(s)
        : restrict to identifier chars + dot/colon/dollar; cap length.
    - clean_command_list(cmds)
        : drop empty/whitespace-only entries (was `_clean_commands_run`).

The existing call sites continue to work because their private helpers
are kept as thin shims that delegate to this module — no behavioral
change, just one source of truth.
"""
from __future__ import annotations
import os
import re
import string
from typing import Iterable, List


# Maximum allowed lengths. Conservative defaults — agents shouldn't be
# pasting megabyte-long names anyway.
MAX_NOTE_BODY_BYTES   = 100_000   # ~25k tokens, generous for prose
MAX_SYMBOL_NAME_CHARS = 256       # SCIP-shaped IDs can be long
MAX_TARGET_CHARS      = 1024


# ---- Path / target ---------------------------------------------------------

def normalize_target_path(s: str) -> str:
    """Canonical form for a CLI-supplied path target.

    - Unifies path separators to `/`
    - Strips a leading `./`
    - Collapses `a/../b` via `os.path.normpath`
    - Hard-caps length so a malicious caller can't blow memory
    """
    if not s:
        return s
    if len(s) > MAX_TARGET_CHARS:
        s = s[:MAX_TARGET_CHARS]
    s = s.replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    s = os.path.normpath(s).replace("\\", "/")
    return s


# Tail-suffix set for `looks_like_path`. Matches what symbols.py used.
_FILE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".swift", ".m", ".mm",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".hh", ".cxx", ".cs",
    ".go", ".rs", ".rb", ".php", ".scala", ".sh",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".css",
    ".md", ".txt", ".sql", ".graphql", ".proto",
)
_FILE_BASENAMES = frozenset({
    "package.json", "tsconfig.json", "pyproject.toml", "setup.py",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Makefile", "Dockerfile", "README.md", "LICENSE",
    ".gitignore", ".dockerignore",
})


def looks_like_path(s: str) -> bool:
    """True when `s` is more naturally interpreted as a file path
    than a bare symbol name."""
    if not s:
        return False
    if "/" in s:
        return True
    if s.endswith(_FILE_SUFFIXES):
        return True
    if s in _FILE_BASENAMES:
        return True
    return False


# Filename-safe character set. Matches POSIX-portable + dash/dot/underscore.
_FILENAME_SAFE = set(string.ascii_letters + string.digits + "._-")


def safe_filename(name: str, *, fallback: str = "pack") -> str:
    """Produce a filesystem-safe basename. Replaces unsafe characters
    with `_`, collapses runs, and limits length to 200 chars."""
    if not name:
        return fallback
    out: List[str] = []
    last_underscore = False
    for ch in name:
        if ch in _FILENAME_SAFE:
            out.append(ch)
            last_underscore = False
        else:
            if not last_underscore:
                out.append("_")
                last_underscore = True
    safe = "".join(out).strip("._-") or fallback
    if len(safe) > 200:
        safe = safe[:200]
    return safe


# ---- Note body ------------------------------------------------------------

# Control characters except common whitespace (TAB, LF, CR).
_CONTROL_RX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_note_body(text: str, *,
                       max_bytes: int = MAX_NOTE_BODY_BYTES) -> str:
    """Strip control chars, normalize line endings, cap length.

    Idempotent. Safe to call on any agent-supplied note body before
    persisting. Does NOT touch markdown or claim syntax — those are
    valid prose."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RX.sub("", text)
    if len(text.encode("utf-8")) > max_bytes:
        # Truncate by char so we don't slice mid-codepoint.
        chars: List[str] = []
        running = 0
        for ch in text:
            running += len(ch.encode("utf-8"))
            if running > max_bytes:
                break
            chars.append(ch)
        text = "".join(chars).rstrip() + "\n[truncated by projmem.security]"
    return text


# ---- Symbol / command -----------------------------------------------------

# What constitutes a "safe" symbol name. Letters, digits, `_`, `.`, `:`,
# `$`, `#`, `/`, `-`, `!`. Covers Python, JS, Java, Go, Rust, SCIP IDs.
_SYMBOL_OK = re.compile(r"[A-Za-z0-9_./:$#!\-]+")


def sanitize_symbol_name(name: str,
                         *, max_chars: int = MAX_SYMBOL_NAME_CHARS) -> str:
    """Restrict to safe identifier characters and cap length.
    Returns "" when the input contains no allowable chars at all."""
    if not name:
        return ""
    if not isinstance(name, str):
        name = str(name)
    keep = "".join(c for c in name if _SYMBOL_OK.fullmatch(c))
    if len(keep) > max_chars:
        keep = keep[:max_chars]
    return keep


def clean_command_list(cmds: Iterable[str]) -> List[str]:
    """Drop empty / whitespace-only entries and de-dupe while preserving
    order. Used when surfacing the `commands_run` array in `projmem ask`."""
    seen: set = set()
    out: List[str] = []
    for c in cmds or []:
        if not c or not c.strip():
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out
