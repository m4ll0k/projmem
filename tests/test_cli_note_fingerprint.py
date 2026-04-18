"""Regression: CLI must not treat an uncomputable fingerprint as captured.

Targets like `@project`, directory prefixes (`src/auth/`), or opaque symbol_ids
do not have a stable file-backed fingerprint. Storing an all-None fingerprint
and marking the note "fresh" is false certainty — these notes must start as
staleness=unknown unless they carry structured claims.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from projmem.cli import main
from projmem.config import Config
from projmem.store import Store


def _run_cli(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


def test_cli_note_add_project_target_does_not_claim_fingerprint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = _run_cli([
        "--path", str(repo),
        "note", "add", "@project",
        "--kind", "note",
        "project-wide context",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["fingerprint_captured"] is False

    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    row = store.list_annotations(target="@project")[0]
    assert row.get("fingerprint") is None
    assert row.get("staleness") == "unknown"
    store.close()


def test_cli_note_add_file_target_captures_fingerprint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = _run_cli([
        "--path", str(repo),
        "note", "add", "a.py#helper",
        "--kind", "note",
        "function is trivial",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["fingerprint_captured"] is True

    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    row = store.list_annotations(target="a.py#helper")[0]
    assert row.get("fingerprint") is not None
    assert row.get("staleness") == "fresh"
    store.close()

