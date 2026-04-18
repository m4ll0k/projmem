"""Output sink for the CLI. The filename is currently hard-coded."""
from __future__ import annotations

from pathlib import Path


def output_path(args) -> Path:
    # TODO: honour --shadow-mode.
    return Path("prod.log")


def write(args, line: str) -> Path:
    p = output_path(args)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")
    return p
