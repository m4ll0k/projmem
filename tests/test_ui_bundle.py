"""Step 6 — UI bundle ships + daemon serves it at /.

Two layers:

  1. Static-asset shape — the committed ``projmem/ui/dist/`` carries an
     index.html + an assets/ directory with at least one .js + one .css.
     Catches the "you forgot to rebuild before committing" failure mode.

  2. Daemon serving — FastAPI route order means the static mount comes
     LAST (otherwise it'd shadow /events / /healthz / /state). We hit
     the daemon with HTTP GET / and assert we get the bundled HTML
     back, while still asserting JSON endpoints work alongside it.

Both layers skip cleanly when daemon optional deps aren't installed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient   # noqa: E402

from projmem import daemon as d              # noqa: E402
from projmem.store import Store              # noqa: E402


UI_DIST = (Path(__file__).parent.parent / "projmem" / "ui" / "dist")


# ---------------------------------------------------------------------------
# Static-asset shape
# ---------------------------------------------------------------------------

def test_ui_dist_is_present():
    assert UI_DIST.is_dir(), (
        "projmem/ui/dist/ is missing. Build it with "
        "`cd projmem/ui && npm install && npm run build`."
    )


def test_ui_dist_has_index_html():
    assert (UI_DIST / "index.html").is_file()


def test_ui_dist_has_assets():
    assets = UI_DIST / "assets"
    assert assets.is_dir()
    files = list(assets.iterdir())
    has_js  = any(f.suffix == ".js"  for f in files)
    has_css = any(f.suffix == ".css" for f in files)
    assert has_js,  "no .js asset under dist/assets/"
    assert has_css, "no .css asset under dist/assets/"


def test_ui_dist_dir_helper_finds_it():
    assert d._ui_dist_dir() == str(UI_DIST)


# ---------------------------------------------------------------------------
# Daemon serves the bundle alongside the API
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    (tmp_path / ".projmem").mkdir()
    s = Store(str(tmp_path / ".projmem" / "index.db")); s.close()
    state = d.DaemonState(str(tmp_path))
    app = d.build_app(state, serve_ui=True)
    with TestClient(app) as c:
        yield c


def test_root_serves_index_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_api_endpoints_still_work_alongside_ui(client):
    # /healthz must NOT have been shadowed by the static mount.
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_assets_path_serves_js():
    # Pick the first .js asset and request it via the daemon. We
    # rebuild the client here so the test stays self-contained.
    js_assets = sorted((UI_DIST / "assets").glob("*.js"))
    assert js_assets, "no JS asset to test"
    asset_name = js_assets[0].name

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".projmem"))
        s = Store(os.path.join(tmp, ".projmem", "index.db")); s.close()
        state = d.DaemonState(tmp)
        app = d.build_app(state, serve_ui=True)
        with TestClient(app) as c:
            r = c.get(f"/assets/{asset_name}")
            assert r.status_code == 200
            assert ("javascript" in r.headers["content-type"]
                    or "ecmascript" in r.headers["content-type"])


def test_serve_ui_false_disables_static_mount(tmp_path):
    (tmp_path / ".projmem").mkdir()
    s = Store(str(tmp_path / ".projmem" / "index.db")); s.close()
    state = d.DaemonState(str(tmp_path))
    app = d.build_app(state, serve_ui=False)
    with TestClient(app) as c:
        # No UI mount → / 404s (or returns FastAPI default).
        r = c.get("/")
        assert r.status_code in (404, 405)
        # API still works.
        r2 = c.get("/healthz")
        assert r2.status_code == 200
