import os
import shutil
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_project")


@pytest.fixture
def fresh_project(tmp_path):
    dst = tmp_path / "sample"
    shutil.copytree(FIXTURE, dst)
    # Clean any previous store
    store_dir = dst / ".projmem"
    for leftover in ("index.db", "packs"):
        p = store_dir / leftover
        if p.exists():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    return str(dst)
