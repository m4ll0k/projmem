"""Regression tests for projmem/artifacts.py and the --include-artifacts
flag on `projmem symbol` / `projmem reverse`.

Contract:
  - classify_file() returns (class, reason) where class ∈ {source, artifact, generated}
  - Directory patterns (dist/, node_modules/, tests/baselines/, changelogs/, ...)
    yield artifact
  - .snap, .bak, .baseline yield artifact
  - .d.ts, .min.js, _pb2.py yield generated
  - CHANGELOG_V6.md yields artifact
  - partition_refs splits correctly on the path key
  - cmd_symbol moves artifact refs to artifact_refs bucket by default
  - --include-artifacts keeps them inline
  - cmd_reverse same contract for reverse/forward deps
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from projmem import artifacts as _art


# ---------------------------------------------------------------------------
# 1. classify_file — pure function
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("src/app.py",                       "source"),
    ("lib/_http_server.js",              "source"),
    ("projmem/store.py",                 "source"),
    ("README.md",                        "source"),
])
def test_classify_source_paths(path, expected):
    cls, _ = _art.classify_file(path)
    assert cls == expected, f"{path} -> {cls}"


@pytest.mark.parametrize("path", [
    "dist/index.js",
    "build/output.js",
    "node_modules/foo/bar.js",
    "vendor/lib/pkg/x.go",
    "third_party/google/protobuf.go",
    "coverage/lcov-report/index.html",
    "tests/baselines/reference/APILibCheck.json",
    "doc/changelogs/CHANGELOG_V6.md",
    "CHANGELOG.md",
    "CHANGELOG_V14.md",
    "__snapshots__/App.test.js.snap",
])
def test_classify_artifact_paths(path):
    cls, reason = _art.classify_file(path)
    assert cls != "source", f"{path} should be artifact, got {cls}"
    assert reason, f"{path} should have a reason"


@pytest.mark.parametrize("path,expected_class", [
    ("foo.d.ts",                  "generated"),
    ("lib/bundle.bundle.js",      "generated"),
    ("app.min.js",                "generated"),
    ("schema_pb2.py",             "generated"),
    ("service_pb2_grpc.py",       "generated"),
    ("api.pb.go",                 "generated"),
    ("build.map",                 "generated"),
])
def test_classify_generated_paths(path, expected_class):
    cls, _ = _art.classify_file(path)
    assert cls == expected_class, f"{path} -> {cls}"


@pytest.mark.parametrize("path", [
    "",
    None,
])
def test_classify_handles_empty(path):
    cls, reason = _art.classify_file(path or "")
    assert cls == "source"


def test_is_artifact_path_bool():
    assert _art.is_artifact_path("CHANGELOG.md")
    assert not _art.is_artifact_path("src/foo.py")
    assert _art.is_artifact_path("dist/main.js")


def test_partition_refs_splits_correctly():
    rows = [
        {"file": "src/app.py",     "line": 10},
        {"file": "CHANGELOG.md",   "line": 20},
        {"file": "tests/test.py",  "line": 30},
        {"file": "dist/bundle.js", "line": 40},
    ]
    source, artifact = _art.partition_refs(rows, path_key="file")
    source_files = {r["file"] for r in source}
    artifact_files = {r["file"] for r in artifact}
    assert source_files == {"src/app.py", "tests/test.py"}
    assert artifact_files == {"CHANGELOG.md", "dist/bundle.js"}


# ---------------------------------------------------------------------------
# 2. cmd_symbol — default excludes artifacts; --include-artifacts restores
# ---------------------------------------------------------------------------

def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


def test_cmd_symbol_partitions_artifact_refs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "doc").mkdir()
    (repo / "doc" / "changelogs").mkdir()
    (repo / "src" / "lib.py").write_text(
        "def SafeThing():\n    return 1\n"
        "def caller():\n    SafeThing()\n")
    # Simulate a CHANGELOG that happens to mention the symbol.
    (repo / "doc" / "changelogs" / "CHANGELOG_V1.md").write_text(
        "* 1.0 — renamed to SafeThing()\n"
        "* 1.1 — SafeThing() robust fix\n")
    (repo / "src" / "extra.py").write_text(
        "from lib import SafeThing\n\ndef boot(): SafeThing()\n")

    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    # Default: artifact refs moved to artifact_refs
    rc, out = _run_cli(["--path", str(repo), "symbol", "SafeThing",
                         "--allow-ambiguous", "--no-implicit-check"])
    assert rc == 0
    data = json.loads(out)
    primary_files = {r["file"] for r in data.get("refs", [])}
    # Source refs only in primary bucket.
    assert not any("CHANGELOG" in f for f in primary_files), (
        f"CHANGELOG refs must not appear in primary refs: {primary_files}")
    # Artifact refs surfaced separately
    if data.get("artifact_refs"):
        artifact_files = {r["file"] for r in data["artifact_refs"]}
        assert any("CHANGELOG" in f for f in artifact_files), (
            f"CHANGELOG refs must appear in artifact_refs: {artifact_files}")
        # Each artifact ref must carry its classification reason.
        for a in data["artifact_refs"]:
            assert a.get("artifact_class")
            assert a.get("artifact_reason")


def test_cmd_symbol_include_artifacts_keeps_them_inline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "lib.py").write_text("def SafeThing():\n    return 1\n")
    (repo / "CHANGELOG.md").write_text("changed SafeThing() semantics\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = _run_cli(["--path", str(repo), "symbol", "SafeThing",
                         "--allow-ambiguous", "--no-implicit-check",
                         "--include-artifacts"])
    assert rc == 0
    data = json.loads(out)
    # Artifact refs must NOT be in a separate bucket when opt-in is set.
    assert "artifact_refs" not in data
    assert "artifact_ref_count" not in data


# ---------------------------------------------------------------------------
# 3. cmd_reverse — same contract for dep lists
# ---------------------------------------------------------------------------

def test_cmd_pack_filters_artifact_deps_by_default(tmp_path):
    """Pack output must mirror cmd_symbol/cmd_reverse: artifact-path
    consumers move to artifact_reverse_dependencies, primary
    reverse_dependencies stays source-only."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "core.py").write_text("x = 1\n")
    (repo / "src" / "consumer.py").write_text("from core import x\n")
    (repo / "CHANGELOG.md").write_text("* mentions src/core.py\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = _run_cli(["--path", str(repo), "pack", "src/core.py"])
    assert rc == 0
    data = json.loads(out)
    primary_files = {r["file"] for r in data.get("reverse_dependencies", [])}
    # CHANGELOG must NOT appear in primary deps.
    assert not any("CHANGELOG" in f for f in primary_files), (
        f"CHANGELOG must not be in primary reverse deps: {primary_files}")


def test_cmd_pack_include_artifacts_keeps_them_inline(tmp_path):
    """--include-artifacts on pack must NOT split the deps list."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "core.py").write_text("x = 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    rc, out = _run_cli(["--path", str(repo), "pack", "src/core.py",
                         "--include-artifacts"])
    data = json.loads(out)
    assert "artifact_reverse_dependencies" not in data
    assert "artifact_direct_dependencies" not in data


def test_cmd_reverse_filters_artifact_deps_by_default(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "core.py").write_text("x = 1\n")
    # Real source importer
    (repo / "src" / "consumer.py").write_text("from core import x\n")
    # Artifact importer (should be filtered)
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.py").write_text("from core import x\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = _run_cli(["--path", str(repo), "reverse", "src/core.py"])
    assert rc == 0
    data = json.loads(out)
    primary = {r["file"] for r in data["reverse_dependencies"]}
    # If the indexer resolves both consumers, the dist one should be
    # separated. If neither resolves, artifact list is empty — both fine.
    for p in primary:
        assert not p.startswith("dist/"), (
            f"dist/ importer must not appear in primary reverse deps: {primary}")
    if data.get("artifact_reverse_dependencies"):
        art = {r["file"] for r in data["artifact_reverse_dependencies"]}
        for p in art:
            assert "dist" in p
