"""Canonical inventory of annotation `kind` values.

The ``annotations.kind`` column accepts any TEXT, but tooling (CLI option
choices, MCP server schema, UI dropdowns) needs a single source of truth
for the kinds the agent and human are expected to use.

Existing v1 kinds (``KINDS_V1``) are kept as-is — every legacy database
will read them back unchanged. v2 adds four guidance-shaped kinds
(``KINDS_V2_GUIDANCE``) that the new ``editing`` verb injects into the
agent's tool-result prelude; ``critical`` is the strongest of these and
carries the cosigner gate.

Adding a kind: extend the matching tuple here and add a regression test.
Removing a kind is not allowed without a migration that retags every
affected row — annotations are persistent across sessions and silent
removal would break recall.
"""
from __future__ import annotations

from typing import Tuple


KINDS_V1: Tuple[str, ...] = (
    "note",
    "refute",
    "verified-safe",
    "documented-footgun",
    "todo",
    "link",
    "risk",
)

KINDS_V2_GUIDANCE: Tuple[str, ...] = (
    "guidance",
    "constraint",
    "preference",
    "critical",
)

KNOWN_KINDS: Tuple[str, ...] = KINDS_V1 + KINDS_V2_GUIDANCE

DEFAULT_KIND: str = "note"


def is_guidance_kind(kind: str) -> bool:
    """True for kinds whose body is injected as a tool-call prelude."""
    return kind in KINDS_V2_GUIDANCE


def is_critical_kind(kind: str) -> bool:
    """True only for the strongest kind; gated by the cosigner check."""
    return kind == "critical"
