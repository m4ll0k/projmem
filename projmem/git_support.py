"""Optional, constrained Git support. Off by default. Only used when explicitly requested."""
from __future__ import annotations
import os
import subprocess
from typing import List, Optional


def is_git(root: str) -> bool:
    return os.path.isdir(os.path.join(root, ".git"))


def recent_commits(root: str, path: str, limit: int = 5) -> List[dict]:
    if not is_git(root):
        return []
    try:
        out = subprocess.check_output(
            ["git", "-C", root, "log", f"-n{limit}", "--pretty=%H%x1f%an%x1f%at%x1f%s", "--", path],
            stderr=subprocess.DEVNULL, text=True, timeout=5)
    except Exception:
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            rows.append({"sha": parts[0], "author": parts[1], "time": int(parts[2]), "subject": parts[3]})
    return rows


def blame_lines(root: str, path: str, start: int, end: int) -> Optional[str]:
    if not is_git(root):
        return None
    try:
        return subprocess.check_output(
            ["git", "-C", root, "blame", "-L", f"{start},{end}", path],
            stderr=subprocess.DEVNULL, text=True, timeout=5)
    except Exception:
        return None
