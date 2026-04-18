"""Round-5 meta-fix: matrix test that exercises every read verb with
known-bad inputs and asserts the structured-error contract.

The contract:
  1. structured `{error: <kebab-tag>, message: <sentence>, ...}` payload
  2. exit code != 0 (specifically 2 for "user error" and 1 for blockers)
  3. JSON is parseable on stdout

This catches the "envelope right, exit wrong" regression class proactively.
A new error path that returns a structured envelope but fails to fire
non-zero exit gets flagged here, not after a real-world report.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout

import pytest

from projmem.cli import main


def _run(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            rc = main(args) or 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    return rc, buf.getvalue()


@pytest.fixture
def indexed_repo(tmp_path):
    """A bare-bones indexed repo to attack with bad inputs."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def x():\n    return 1\n")
    rc, _ = _run(["--path", str(root), "index"])
    assert rc == 0
    return str(root)


# Matrix: (label, argv, expected_error_tag, expected_rc).
# Every row exercises one structured-error path. Adding a new error
# path? Add a row here so the contract is mechanically enforced.
ERROR_MATRIX = [
    ("task close bogus id",
     lambda root: ["--path", root, "task", "close", "99999"],
     "task-not-found", 2),
    ("note delete bogus id",
     lambda root: ["--path", root, "note", "delete", "99999"],
     "note-not-found", 2),
    ("search empty query",
     lambda root: ["--path", root, "search", ""],
     "empty-query", 2),
    ("snapshot empty label",
     lambda root: ["--path", root, "snapshot", ""],
     "empty-snapshot-label", 2),
    ("contract-diff missing snapshot",
     lambda root: ["--path", root, "contract-diff", "--base", "ghost"],
     "snapshot-not-found", 2),
    ("symbol-diff missing snapshot",
     lambda root: ["--path", root, "symbol-diff", "--base", "ghost"],
     "snapshot-not-found", 2),
    ("note add empty body",
     lambda root: ["--path", root, "note", "add",
                   "a.py", "--kind", "note", ""],
     "empty-body", 2),
    ("evidence path traversal",
     lambda root: ["--path", root, "note", "add",
                   "a.py", "--kind", "note", "x",
                   "--evidence", "../../../etc/passwd:1"],
     "evidence-out-of-repo", 2),
    ("invalid claims json",
     lambda root: ["--path", root, "note", "add",
                   "a.py", "--kind", "note", "x",
                   "--claims", "not-valid-json"],
     "invalid-claims-json", 2),
    ("session unknown target",
     lambda root: ["--path", root, "session", "no_such_thing"],
     "target-not-found", 2),
    ("note-import missing file",
     lambda root: ["--path", root, "note-import", "/no/such/file.jsonl"],
     "read-failed", 2),
    ("guide unknown topic",
     lambda root: ["--json", "guide", "no-such-topic"],
     "unknown-topic", 2),
    ("trace without --experimental",
     lambda root: ["--path", root, "trace", "src", "sink"],
     "trace-requires-experimental-opt-in", 2),
    ("note-verify unknown target",
     lambda root: ["--path", root, "note-verify", "no_such_target"],
     "target-not-found", 2),
    ("evidence-query unknown target",
     lambda root: ["--path", root, "evidence-query", "no_such_target"],
     "target-not-found", 2),
    ("search invalid limit",
     lambda root: ["--path", root, "search", "x", "--limit", "-1"],
     "invalid-limit", 2),
]


@pytest.mark.parametrize("label,build_args,expected_tag,expected_rc",
                          ERROR_MATRIX,
                          ids=[row[0] for row in ERROR_MATRIX])
def test_error_envelope_contract(indexed_repo, label, build_args,
                                   expected_tag, expected_rc):
    """Every error envelope must (a) parse as JSON on stdout, (b) carry
    the canonical `error` tag, (c) exit with the expected non-zero rc.
    """
    args = build_args(indexed_repo)
    rc, out = _run(args)
    # Contract bit 1: stdout parses as JSON.
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"{label}: stdout did not parse as JSON. rc={rc}, "
            f"out={out[:200]!r}, err={e}")
    # Contract bit 2: canonical kebab-case tag.
    assert data.get("error") == expected_tag, (
        f"{label}: expected error tag {expected_tag!r}; got "
        f"{data.get('error')!r}. Full payload: {data}")
    # Contract bit 3: non-zero exit code matches expectation.
    assert rc == expected_rc, (
        f"{label}: expected exit {expected_rc}, got {rc}. "
        f"Envelope shape was right but the exit code lied — this is "
        f"the round-5 meta-bug class.")


def test_error_envelope_carries_message_field(indexed_repo):
    """Every error envelope should carry a `message` (human-readable
    sentence) — distinct from `error` (kebab-case tag). Lacking the
    sentence forces callers to grok the tag, defeating the point of
    a structured contract."""
    rc, out = _run(["--path", indexed_repo, "task", "close", "99999"])
    data = json.loads(out)
    assert "message" in data, (
        f"task-not-found envelope is missing `message`: {data}")
    assert isinstance(data["message"], str) and data["message"]


def test_no_index_error_carries_path(tmp_path):
    """Round-4 #3: pointing read commands at an unindexed root must
    surface a structured no-index payload that names the path."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    rc, out = _run(["--path", str(fresh), "stats"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "no-index"
    assert data["path"] == str(fresh)
