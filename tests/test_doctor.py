"""Regression tests for projmem doctor.

Contract:
  - run(cfg, store) returns dict with `schema_version`, `overall`,
    `severity_counts`, `findings` (list).
  - Findings sorted by severity (HIGH first, then WARNING, then INFO).
  - Each finding carries `severity`, `code`, `message`, optional `suggestion`,
    optional `details`.
  - Empty index triggers `empty_index` (HIGH).
  - Foreign index triggers `foreign_index` (HIGH).
  - Artifact bleed >20% triggers `artifact_bleed_high` (WARNING).
  - Stale file sampling surfaces `stale_files_on_disk`.
  - CLI: exit code 1 on HIGH, 0 otherwise.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout

import pytest

from projmem import doctor
from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store


def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Core run()
# ---------------------------------------------------------------------------

def test_doctor_returns_schema_version_and_findings(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    report = doctor.run(cfg, store)
    store.close()

    assert report["schema_version"] == 1
    assert "overall" in report
    assert "severity_counts" in report
    assert "findings" in report
    assert isinstance(report["findings"], list)


def test_doctor_empty_index_flags_high(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    # No index_all — DB is empty.
    report = doctor.run(cfg, store)
    store.close()
    codes = {f["code"] for f in report["findings"]}
    assert "empty_index" in codes
    empty = next(f for f in report["findings"] if f["code"] == "empty_index")
    assert empty["severity"] == "high"


def test_doctor_ok_on_healthy_small_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def ok():\n    return 1\n")
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    report = doctor.run(cfg, store)
    store.close()
    # No HIGH, maybe INFO entries only.
    assert report["severity_counts"]["high"] == 0
    assert report["overall"] in ("ok", "attention_needed")


def test_doctor_detects_stale_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    fp = root / "a.py"
    fp.write_text("x = 1\n")
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    # Modify the file — hash drift.
    fp.write_text("x = 2\n")
    report = doctor.run(cfg, store)
    store.close()
    codes = {f["code"] for f in report["findings"]}
    assert "stale_files_on_disk" in codes
    stale = next(f for f in report["findings"]
                 if f["code"] == "stale_files_on_disk")
    assert stale["severity"] in ("warning", "high")
    assert "a.py" in stale["details"]["example_paths"]


def test_doctor_detects_artifact_bleed(tmp_path):
    """Repo with >20% artifact files triggers `artifact_bleed_high`.

    `dist/`, `node_modules/`, etc. are pruned by the walker before indexing,
    so this test uses file-pattern artifacts (CHANGELOG markdown, .snap
    snapshots, .d.ts declarations) that DO get indexed but are classified
    as artifacts by projmem/artifacts.py.
    """
    root = tmp_path / "repo"
    root.mkdir()
    # 4 source files
    for i in range(4):
        (root / f"src{i}.py").write_text(f"x{i} = {i}\n")
    # 5 artifact files that pass the directory walker but classify as
    # artifacts (CHANGELOG_*.md + .snap + .d.ts)
    (root / "CHANGELOG_V1.md").write_text("* 1.0\n")
    (root / "CHANGELOG_V2.md").write_text("* 2.0\n")
    (root / "CHANGELOG_V3.md").write_text("* 3.0\n")
    (root / "App.test.js.snap").write_text("exports[`test 1`] = `1`;\n")
    (root / "types.d.ts").write_text("export type X = number;\n")
    # 9 total, 5 artifacts = 55.6% → should trigger artifact_bleed_high
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    report = doctor.run(cfg, store)
    store.close()
    codes = {f["code"] for f in report["findings"]}
    assert "artifact_bleed_high" in codes, (
        f"Expected artifact_bleed_high; got {codes}")


def test_doctor_sorts_findings_by_severity(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    # Empty index → empty_index HIGH
    report = doctor.run(cfg, store)
    store.close()
    severities = [f["severity"] for f in report["findings"]]
    # HIGH must come before WARNING / INFO
    last_order = -1
    severity_order = {"high": 0, "warning": 1, "info": 2}
    for s in severities:
        assert severity_order[s] >= last_order
        last_order = severity_order[s]


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

def test_cli_doctor_exit_0_when_healthy(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(root), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(root), "doctor"])
    data = json.loads(out)
    assert rc == 0
    assert data["severity_counts"]["high"] == 0


def test_cli_doctor_no_index_returns_no_index_error(tmp_path):
    """Round-4 #3: doctor on an unindexed root returns the standardized
    `no-index` payload (exit 2). Previously emitted a `severity=high`
    `empty_index` finding (exit 1), which conflated "you forgot to
    index" with "your index is sick"."""
    root = tmp_path / "repo"
    root.mkdir()
    os.makedirs(str(root / ".projmem"), exist_ok=True)
    Store(str(root / ".projmem" / "index.db"))
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            from projmem.cli import main
            main(["--path", str(root), "doctor"])
            rc = 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    data = json.loads(buf.getvalue())
    assert rc == 2
    assert data["error"] == "no-index"


def test_cli_doctor_skip_stale_check(tmp_path):
    """--skip-stale-check must suppress the stale sampling."""
    root = tmp_path / "repo"
    root.mkdir()
    fp = root / "a.py"
    fp.write_text("x = 1\n")
    rc, _ = _run_cli(["--path", str(root), "index"])
    assert rc == 0
    fp.write_text("x = 2\n")
    rc, out = _run_cli(["--path", str(root), "doctor", "--skip-stale-check"])
    data = json.loads(out)
    codes = {f["code"] for f in data["findings"]}
    assert "stale_files_on_disk" not in codes
