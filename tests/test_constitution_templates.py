"""Step 4 — assert the rewritten CLAUDE.md / AGENTS.md templates carry
the constitution markers the brief requires.

The brief mandates several elements appear verbatim:
  * "If projmem is unavailable, STOP"
  * "contradicted_count > 0" with explicit STOP
  * The seven v1 verbs listed
  * The four v2 mutation verbs listed
  * ⚠ CRITICAL CONTEXT handling instructions
"""
from __future__ import annotations

from pathlib import Path

import pytest


CLAUDE_MD = (
    Path(__file__).parent.parent / "projmem" / "templates" / "CLAUDE.md"
)
AGENTS_MD = (
    Path(__file__).parent.parent / "projmem" / "templates" / "AGENTS.md"
)


SEVEN_V1_VERBS = (
    "projmem note add",
    "projmem notes",
    "projmem session",
    "projmem conclude",
    "projmem fact-check",
    "projmem task",
    "projmem refresh",
)

FOUR_V2_VERBS = (
    "projmem editing",
    "projmem creating",
    "projmem moving",
    "projmem deleting",
)

REQUIRED_PHRASES = (
    "If projmem is unavailable",
    "contradicted_count > 0",
    "STOP",
    "⚠ CRITICAL CONTEXT",
    "projmem complete || exit 2",
    "lease",
    "reason",
)


@pytest.mark.parametrize("template_path", [CLAUDE_MD, AGENTS_MD])
def test_template_contains_required_constitution_markers(template_path):
    text = template_path.read_text(encoding="utf-8")
    for phrase in REQUIRED_PHRASES:
        assert phrase in text, (
            f"{template_path.name} is missing required marker: {phrase!r}"
        )


@pytest.mark.parametrize("template_path", [CLAUDE_MD, AGENTS_MD])
def test_template_lists_every_v1_verb(template_path):
    text = template_path.read_text(encoding="utf-8")
    for verb in SEVEN_V1_VERBS:
        assert verb in text, (
            f"{template_path.name} is missing v1 verb: {verb!r}"
        )


@pytest.mark.parametrize("template_path", [CLAUDE_MD, AGENTS_MD])
def test_template_lists_every_v2_mutation_verb(template_path):
    text = template_path.read_text(encoding="utf-8")
    for verb in FOUR_V2_VERBS:
        assert verb in text, (
            f"{template_path.name} is missing v2 verb: {verb!r}"
        )


def test_claude_md_is_imperative_not_advisory():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    # The brief says "imperatives only" — the old advisory language
    # we replaced. Quick smoke check: imperative markers should
    # outnumber "Anything else is optional" type language.
    assert "Non-negotiables" in text or "non-negotiable" in text.lower()
    assert "Anything else is optional" not in text


def test_critical_prelude_handling_is_three_step_required():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    # The brief mandates: state intent, confirm scope, halt if overlap.
    assert "State the intended change" in text or "state the intended change" in text.lower()
    assert "Halt" in text or "halt" in text
