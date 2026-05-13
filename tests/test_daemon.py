"""Step 5 — local daemon (FastAPI + WebSocket).

Tests run the daemon's FastAPI app through ``fastapi.testclient.TestClient``
so we exercise the actual route handlers without spinning a uvicorn
server. The loopback-bind paranoia check runs against the
``_assert_loopback`` helper directly.

All tests skip cleanly when the daemon optional deps aren't installed.
"""
from __future__ import annotations

import os
import time

import pytest


pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient   # noqa: E402

from projmem import daemon as d              # noqa: E402
from projmem.store import Store              # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A repo root with a fresh .projmem/ index seeded."""
    (tmp_path / ".projmem").mkdir()
    s = Store(str(tmp_path / ".projmem" / "index.db"))
    s.upsert_file("src/a.py", "py", "h", time.time(), 1, "ast")
    s.conn.commit()
    s.close()
    return tmp_path


@pytest.fixture
def client(repo):
    state = d.DaemonState(str(repo))
    app = d.build_app(state)
    with TestClient(app) as c:
        yield c, state


# ---------------------------------------------------------------------------
# Paranoid loopback-only bind check
# ---------------------------------------------------------------------------

class TestBindLoopback:
    def test_loopback_accepted(self):
        d._assert_loopback("127.0.0.1")
        d._assert_loopback("localhost")
        d._assert_loopback("::1")

    def test_zero_zero_zero_zero_refused(self):
        with pytest.raises(d.BindRefusedError):
            d._assert_loopback("0.0.0.0")

    def test_public_ip_refused(self):
        with pytest.raises(d.BindRefusedError):
            d._assert_loopback("8.8.8.8")


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class TestHttpEndpoints:
    def test_healthz(self, client):
        c, _ = client
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_state_lists_open_leases(self, client):
        c, state = client
        # Open a lease via the underlying mutation_verbs so /state has
        # something to report.
        from projmem import mutation_verbs as _mv
        store = state.store()
        _mv.open_editing_lease(
            store, "src/a.py",
            reason="exercising the daemon state endpoint",
        )
        store.close()
        r = c.get("/state")
        assert r.status_code == 200
        body = r.json()
        assert len(body["open_leases"]) == 1

    def test_post_note(self, client):
        c, state = client
        r = c.post("/notes", json={
            "target":   "src/a.py",
            "kind":     "guidance",
            "severity": "warn",
            "body":     "prefer functional style here",
        })
        assert r.status_code == 200
        assert r.json()["id"]
        # The daemon's in-memory buffer recorded the event.
        assert any(ev["kind"] == "note_added" for ev in state.events)

    def test_serve_socket_survives_missing_parent_dir(self, tmp_path, capsys):
        # Regression: uvloop on Python 3.14 surfaced a bare
        # FileNotFoundError when serve_socket raced startup; the task
        # crashed with "Task exception was never retrieved" while the
        # rest of the daemon kept serving. Hardened path must self-heal
        # via makedirs + wrap bind in try/except.
        import asyncio
        state = d.DaemonState(str(tmp_path))   # tmp_path has no .projmem/

        async def run():
            # Should NOT raise — it must create .projmem/ and bind, OR
            # log a warning and return cleanly. Either way, no uncaught
            # FileNotFoundError propagates out of the task.
            task = asyncio.create_task(d.serve_socket(state))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(run())

        # `.projmem/` must now exist (created by the defensive makedirs).
        assert (tmp_path / ".projmem").is_dir()

    def test_inspector_critical_categories_match_backend(self):
        # The /critical endpoint rejects categories outside
        # projmem.critical.CATEGORIES with a 400 + CategoryError envelope.
        # The inspector form has a hard-coded list — if the two drift
        # again the add-critical button looks broken, so pin the
        # canonical set here. Anyone adding a category must update both.
        from projmem.critical import CATEGORIES
        ui_inspector = (
            "projmem/ui/src/components/Inspector.tsx"
        )
        with open(ui_inspector, encoding="utf-8") as f:
            src = f.read()
        for c in CATEGORIES:
            assert f'"{c}"' in src, (
                f"category {c!r} from projmem.critical.CATEGORIES "
                "missing from AddCriticalForm — UI 400s will result"
            )

    def test_critical_400_envelope_has_message_field(self, client):
        # The UI surfaces the error envelope's `message` to the user;
        # this pins the daemon's shape so a refactor can't strip it.
        c, _ = client
        r = c.post("/critical", json={
            "target":      "src/a.py",
            "reason":      "too short",
            "category":    "security",
            "self_cosign": True,
        })
        assert r.status_code == 400
        body = r.json()
        assert isinstance(body.get("message"), str) and body["message"]

    def test_patch_note_updates_body_and_broadcasts(self, client):
        c, state = client
        post = c.post("/notes", json={
            "target": "src/a.py", "kind": "guidance", "body": "v1",
            "severity": "info",
        })
        ann_id = post.json()["id"]

        r = c.patch(f"/notes/{ann_id}", json={"body": "v2", "severity": "warn"})
        assert r.status_code == 200, r.text
        assert r.json()["updated"] is True
        assert any(ev["kind"] == "note_edited" and ev["id"] == ann_id
                   for ev in state.events)

    def test_patch_note_404_on_unknown(self, client):
        c, _ = client
        r = c.patch("/notes/99999", json={"body": "x"})
        assert r.status_code == 404

    def test_patch_note_rejects_empty_body(self, client):
        c, _ = client
        post = c.post("/notes", json={
            "target": "src/a.py", "kind": "note", "body": "v1",
        })
        ann_id = post.json()["id"]
        r = c.patch(f"/notes/{ann_id}", json={"body": "   "})
        assert r.status_code == 400

    def test_resolve_path_returns_lifeline_id(self, client):
        c, _ = client
        r = c.get("/resolve-path", params={"path": "src/a.py"})
        assert r.status_code == 200
        body = r.json()
        assert body["current_path"] == "src/a.py"
        assert body["lifeline_id"]

    def test_resolve_path_404_for_unknown(self, client):
        c, _ = client
        r = c.get("/resolve-path", params={"path": "no/such/file.py"})
        assert r.status_code == 404

    def test_delete_note_removes_row_and_broadcasts(self, client):
        # Regression for the v2 UI delete-button — every note kind
        # (note / guidance / exclude / critical) is one row in the
        # annotations table, so a single DELETE /notes/{id} reaches all
        # of them. The UI calls this on every trash-can click.
        c, state = client
        post = c.post("/notes", json={
            "target": "src/a.py", "kind": "note", "body": "scratch",
        })
        assert post.status_code == 200
        ann_id = post.json()["id"]

        r = c.delete(f"/notes/{ann_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == ann_id and body["deleted"] is True
        assert any(
            ev["kind"] == "note_deleted" and ev["id"] == ann_id
            for ev in state.events
        )

    def test_delete_note_404_on_unknown(self, client):
        c, _ = client
        r = c.delete("/notes/99999")
        assert r.status_code == 404
        assert r.json()["error"] == "not-found"

    def test_delete_exclusion_makes_subtree_in_scope_again(self, client, repo):
        # The deeper user-reported behavior: removing a kind=exclude
        # row must drop the OUT OF SCOPE warning for files under that
        # subtree on the very next lease. Without this, the UI "remove
        # exclusion" button would be cosmetic.
        from projmem.mutation_verbs import _exclusion_ancestors
        from projmem.store import Store

        c, _ = client
        post = c.post("/notes", json={
            "target": "src/legacy/",
            "kind":   "exclude",
            "body":   "vendored — do not edit",
        })
        ex_id = post.json()["id"]

        st = Store(str(repo / ".projmem" / "index.db"))
        try:
            hit = _exclusion_ancestors(st.conn, "src/legacy/util.py")
            assert any(r["target"] == "src/legacy/" for r in hit), hit
        finally:
            st.close()

        c.delete(f"/notes/{ex_id}")

        st = Store(str(repo / ".projmem" / "index.db"))
        try:
            hit_after = _exclusion_ancestors(st.conn, "src/legacy/util.py")
            assert not hit_after, hit_after
        finally:
            st.close()

    def test_exclusion_covers_deep_descendants(self, client, repo):
        # The user thought exclusions only applied to "the last node",
        # so this pins down recursive scope: an exclude at
        # `tests/fixtures/multilang/go/util/` matches files several
        # levels deeper. _exclusion_ancestors is the function that
        # the editing-lease mutation calls before surfacing warnings.
        from projmem.mutation_verbs import _exclusion_ancestors
        from projmem.store import Store

        c, _ = client
        c.post("/notes", json={
            "target": "tests/fixtures/multilang/go/util/",
            "kind":   "exclude",
            "body":   "go test fixture — no real-codebase value",
        })
        st = Store(str(repo / ".projmem" / "index.db"))
        try:
            for nested in [
                "tests/fixtures/multilang/go/util/util.go",
                "tests/fixtures/multilang/go/util/subdir/deep.go",
                "tests/fixtures/multilang/go/util/a/b/c/d.go",
            ]:
                hit = _exclusion_ancestors(st.conn, nested)
                assert any(
                    r["target"] == "tests/fixtures/multilang/go/util/"
                    for r in hit
                ), (nested, hit)
        finally:
            st.close()

    def test_post_critical_missing_cosigner_returns_400(self, client):
        c, _ = client
        r = c.post("/critical", json={
            "target":   "src/a.py",
            "reason":   ("A long reason describing the constraint, the "
                          "incident, and the consequence — over 40 chars."),
            "category": "security",
        })
        assert r.status_code == 400
        assert r.json()["error"] == "cosigner-required"

    def test_post_critical_with_self_cosign_succeeds(self, client):
        c, _ = client
        r = c.post("/critical", json={
            "target":      "src/a.py",
            "reason":      ("A long reason describing the constraint, the "
                              "incident, and the consequence — over 40 chars."),
            "category":    "security",
            "self_cosign": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["category"] == "security"
        assert body["approved_by"] == ["@self-cosign"]


# ---------------------------------------------------------------------------
# Pause / resume / approve / deny
# ---------------------------------------------------------------------------

class TestPauseAndApprove:
    def test_pause_resume_flips_state(self, client):
        c, state = client
        assert state.paused is False
        c.post("/control/pause")
        assert state.paused is True
        c.post("/control/resume")
        assert state.paused is False

    def test_approve_promotes_pending_lease_to_open(self, client):
        c, state = client
        # Set up a critical note + open a lease (lands as pending_approval).
        from projmem import critical as _crit, mutation_verbs as _mv
        store = state.store()
        _crit.add_critical(
            store, "src/a.py",
            reason=("A long reason describing the constraint, the incident, "
                      "and the consequence — over 40 chars."),
            category="security", self_cosign=True,
        )
        lease = _mv.open_editing_lease(
            store, "src/a.py",
            reason="critical edit attempt that should block",
        )
        store.close()
        assert lease["lease_state"] == "pending_approval"

        r = c.post(f"/control/approve/{lease['lease_id']}")
        assert r.status_code == 200
        # Lease state now 'open'.
        store2 = state.store()
        row = store2.conn.execute(
            "SELECT state FROM edit_lease WHERE id=?",
            (lease["lease_id"],),
        ).fetchone()
        store2.close()
        assert row["state"] == "open"

    def test_deny_closes_lease(self, client):
        c, state = client
        from projmem import critical as _crit, mutation_verbs as _mv
        store = state.store()
        _crit.add_critical(
            store, "src/a.py",
            reason=("A long reason describing the constraint, the incident, "
                      "and the consequence — over 40 chars."),
            category="security", self_cosign=True,
        )
        lease = _mv.open_editing_lease(
            store, "src/a.py",
            reason="critical edit that should be denied",
        )
        store.close()
        r = c.post(f"/control/deny/{lease['lease_id']}")
        assert r.status_code == 200
        store2 = state.store()
        row = store2.conn.execute(
            "SELECT state, closed_kind FROM edit_lease WHERE id=?",
            (lease["lease_id"],),
        ).fetchone()
        store2.close()
        assert row["state"] == "closed"
        assert row["closed_kind"] == "abandoned"


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

class TestGraphEndpoint:
    def test_graph_returns_nodes_and_edges(self, client):
        c, state = client
        store = state.store()
        store.upsert_file("src/b.py", "py", "h", time.time(), 1, "ast")
        store.conn.execute(
            "INSERT INTO edges(src, dst, type) "
            "VALUES('src/b.py', 'src/a.py', 'imports')",
        )
        store.conn.commit()
        store.close()
        r = c.get("/graph")
        assert r.status_code == 200
        g = r.json()
        paths = {n["path"] for n in g["nodes"]}
        assert {"src/a.py", "src/b.py"} <= paths
        assert any(e["kind"] == "imports" for e in g["edges"])
        assert g["include_ghosts"] is False

    def test_graph_with_include_ghosts_shows_tombstones(self, client):
        from projmem import mutation_verbs as _mv
        c, state = client
        store = state.store()
        _mv.delete_path(
            store, "src/a.py",
            reason="consolidated into shared/util.py",
            replaced_by=["src/a.py"],  # self-replacement is fine for test
        )
        store.close()
        # Without ghosts, src/a.py is gone from the graph.
        no_ghosts = c.get("/graph?include_ghosts=0").json()
        active_paths = {n["path"] for n in no_ghosts["nodes"] if not n["ghost"]}
        assert "src/a.py" not in active_paths

        # With ghosts, the tombstoned lifeline reappears as a ghost.
        with_ghosts = c.get("/graph?include_ghosts=1").json()
        ghost_paths = {n["path"] for n in with_ghosts["nodes"] if n["ghost"]}
        assert "src/a.py" in ghost_paths
        # Tombstoned reason rides on the node.
        ghost = next(n for n in with_ghosts["nodes"]
                     if n["ghost"] and n["path"] == "src/a.py")
        assert "consolidated" in (ghost.get("tombstoned_reason") or "")


class TestRefsEndpoint:
    """`.projmem/refs/` lets the operator drop research material
    (papers, design docs, PDFs) that notes can `[link]()` to. The
    daemon serves these read-only with a path-traversal guard."""

    def test_lists_empty_when_no_refs_dir(self, client):
        c, _ = client
        r = c.get("/refs-list")
        assert r.status_code == 200
        d = r.json()
        assert d["root_exists"] is False
        assert d["refs"] == []

    def test_serves_a_real_file(self, client, repo):
        c, _ = client
        refs = repo / ".projmem" / "refs" / "papers"
        refs.mkdir(parents=True)
        (refs / "SEC-204.md").write_text("# SEC-204 advisory\n\nDo X.\n")
        r = c.get("/refs/papers/SEC-204.md")
        assert r.status_code == 200
        assert "SEC-204 advisory" in r.text
        # Listing surfaces it.
        lst = c.get("/refs-list").json()
        assert any(x["path"] == "papers/SEC-204.md" for x in lst["refs"])

    def test_path_traversal_is_refused(self, client, repo):
        c, _ = client
        (repo / ".projmem" / "refs").mkdir(parents=True)
        r = c.get("/refs/..%2F..%2Fetc%2Fpasswd")
        # Either 400 (caught by our guard) or 404 (FastAPI's path
        # parameter rejected the encoded slashes); both are correct
        # from a security standpoint. The MUST-NOT is "200 with the
        # file's contents."
        assert r.status_code in (400, 404)


class TestLifelineEndpoint:
    def test_lifeline_returns_full_detail(self, client):
        from projmem import mutation_verbs as _mv
        c, state = client
        store = state.store()
        # Add a guidance note on src/a.py + open a lease so events accumulate.
        store.add_annotation(
            target="src/a.py", kind="guidance",
            body="prefer functional style here", severity="warn",
        )
        _mv.open_editing_lease(
            store, "src/a.py",
            reason="exercising the lifeline detail endpoint",
        )
        lifeline_id = store.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", ("src/a.py",),
        ).fetchone()["lifeline_id"]
        store.close()

        r = c.get(f"/lifeline/{lifeline_id}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["lifeline"]["id"] == lifeline_id
        assert detail["lifeline"]["current_path"] == "src/a.py"
        assert any(n["kind"] == "guidance" for n in detail["notes"])
        # At least the 'created' (from upsert), 'leased' events present.
        kinds = {e["kind"] for e in detail["events"]}
        assert "leased" in kinds

    def test_lifeline_404_on_unknown(self, client):
        c, _ = client
        r = c.get("/lifeline/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404


class TestWebSocket:
    def test_events_endpoint_broadcasts_to_subscriber(self, client):
        c, state = client
        # Pre-buffer a recorded event so the replay path is exercised.
        state.record({"kind": "synthetic_pre_event"})
        with c.websocket_connect("/events") as ws:
            # First message is the replay of buffered events.
            replayed = ws.receive_json()
            assert replayed["kind"] == "synthetic_pre_event"
            # Now POST a note and assert the subscriber sees the
            # live broadcast.
            c.post("/notes", json={
                "target": "src/a.py", "body": "live broadcast test",
            })
            live = ws.receive_json()
            assert live["kind"] == "note_added"
