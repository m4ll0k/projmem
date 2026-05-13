"""Coverage for v2.1 skills (promoted from scaffold to full impl)."""
from __future__ import annotations

import time

import pytest

from projmem import mutation_verbs as mv, skills as sk
from projmem.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / ".projmem" / "index.db"))
    yield s
    s.close()


def _seed(store, path):
    store.upsert_file(path, "py", "h", time.time(), 1, "ast")
    store.conn.commit()


GOOD_PROMPT = (
    "Use lateral thinking: do not commit to your first solution. "
    "Generate alternatives that challenge baked-in assumptions."
)


# ---------------------------------------------------------------------------
# add / list / edit / disable
# ---------------------------------------------------------------------------

class TestAddListEditDisable:
    def test_add_rejects_short_prompt(self, store):
        with pytest.raises(sk.InvalidSkillFieldError):
            sk.add_skill(store, name="x", prompt="short", scope_pattern="**")

    def test_add_rejects_bad_trigger(self, store):
        with pytest.raises(sk.InvalidSkillFieldError):
            sk.add_skill(store, name="x", prompt=GOOD_PROMPT,
                         scope_pattern="**", trigger="whenever")

    def test_add_rejects_bad_inject_as(self, store):
        with pytest.raises(sk.InvalidSkillFieldError):
            sk.add_skill(store, name="x", prompt=GOOD_PROMPT,
                         scope_pattern="**", inject_as="shouty")

    def test_add_rejects_duplicate_name(self, store):
        sk.add_skill(store, name="lat", prompt=GOOD_PROMPT, scope_pattern="**")
        with pytest.raises(sk.SkillNameConflictError):
            sk.add_skill(store, name="lat", prompt=GOOD_PROMPT,
                         scope_pattern="**")

    def test_list_returns_added_skill(self, store):
        sk.add_skill(store, name="lat", prompt=GOOD_PROMPT, scope_pattern="**")
        names = [s["name"] for s in sk.list_skills(store)]
        assert "lat" in names

    def test_disable_then_enable_round_trip(self, store):
        sk.add_skill(store, name="x", prompt=GOOD_PROMPT, scope_pattern="**")
        sk.disable_skill(store, "x", enabled=False)
        assert sk.list_skills(store, include_disabled=False) == []
        sk.disable_skill(store, "x", enabled=True)
        assert len(sk.list_skills(store, include_disabled=False)) == 1

    def test_edit_updates_prompt(self, store):
        sk.add_skill(store, name="x", prompt=GOOD_PROMPT, scope_pattern="**")
        sk.edit_skill(store, "x", prompt=GOOD_PROMPT + " edited")
        assert "edited" in sk.list_skills(store)[0]["prompt"]


# ---------------------------------------------------------------------------
# Gitignore-style glob matching (the bug fixed via dogfood)
# ---------------------------------------------------------------------------

class TestGlobMatch:
    def test_doublestar_matches_shallow_paths(self):
        # The classic gitignore behavior: `projmem/**/*.py` matches
        # `projmem/store.py` (with `**` matching zero segments).
        assert sk._glob_match("projmem/**/*.py", "projmem/store.py")

    def test_doublestar_matches_deep_paths(self):
        assert sk._glob_match("projmem/**/*.py",
                              "projmem/migrations/m001.py")

    def test_specific_pattern_still_works(self):
        assert sk._glob_match("tests/**", "tests/foo/bar.py")
        assert not sk._glob_match("tests/**", "src/foo.py")

    def test_bare_doublestar_matches_anything(self):
        assert sk._glob_match("**", "any/path/here.py")
        assert sk._glob_match("**/*", "x.py")


# ---------------------------------------------------------------------------
# skills_for_path + attach/detach
# ---------------------------------------------------------------------------

class TestSkillsForPath:
    def test_pattern_match_surfaces_skill(self, store):
        _seed(store, "src/auth.py")
        sk.add_skill(store, name="lat", prompt=GOOD_PROMPT,
                     scope_pattern="src/**/*.py", trigger="on_edit")
        rows = sk.skills_for_path(store, "src/auth.py", trigger="on_edit")
        assert any(r["name"] == "lat" for r in rows)
        assert rows[0]["match_via"] == "pattern"

    def test_trigger_filter_excludes_non_matching(self, store):
        _seed(store, "src/a.py")
        sk.add_skill(store, name="read-only", prompt=GOOD_PROMPT,
                     scope_pattern="**", trigger="on_read")
        rows = sk.skills_for_path(store, "src/a.py", trigger="on_edit")
        assert all(r["name"] != "read-only" for r in rows)

    def test_always_trigger_always_fires(self, store):
        _seed(store, "src/a.py")
        sk.add_skill(store, name="ever", prompt=GOOD_PROMPT,
                     scope_pattern="**", trigger="always")
        for t in ("on_edit", "on_read", "on_create"):
            assert any(r["name"] == "ever" for r in
                       sk.skills_for_path(store, "src/a.py", trigger=t))

    def test_attach_then_skill_matches_via_attached(self, store):
        _seed(store, "src/lonely.py")
        sk.add_skill(store, name="pin", prompt=GOOD_PROMPT,
                     scope_pattern="src/never_matches/**",
                     trigger="on_edit")
        sk.attach_skill(store, "pin", "src/lonely.py")
        rows = sk.skills_for_path(store, "src/lonely.py", trigger="on_edit")
        assert rows[0]["match_via"] == "attached"

    def test_detach_removes_attachment(self, store):
        _seed(store, "src/a.py")
        sk.add_skill(store, name="pin", prompt=GOOD_PROMPT,
                     scope_pattern="never/match",
                     trigger="on_edit")
        sk.attach_skill(store, "pin", "src/a.py")
        r = sk.detach_skill(store, "pin", "src/a.py")
        assert r["removed"] == 1
        rows = sk.skills_for_path(store, "src/a.py", trigger="on_edit")
        assert all(r2["name"] != "pin" for r2 in rows)


# ---------------------------------------------------------------------------
# Injection — `editing` response carries skills[] + skill_prelude
# ---------------------------------------------------------------------------

class TestSkillsInjectIntoEditing:
    def test_skill_prelude_appears_in_editing_response(self, store):
        _seed(store, "src/payment.py")
        sk.add_skill(store, name="payments-care",
                     prompt=GOOD_PROMPT, scope_pattern="src/**/*.py",
                     trigger="on_edit", inject_as="system")
        result = mv.open_editing_lease(
            store, "src/payment.py",
            reason="touching the payment flow with care",
        )
        assert any(s["name"] == "payments-care"
                   for s in result.get("skills", []))
        assert "skill_prelude" in result
        assert "Methodology override" in result["skill_prelude"]

    def test_no_skill_no_prelude(self, store):
        _seed(store, "src/a.py")
        result = mv.open_editing_lease(
            store, "src/a.py",
            reason="exercising the no-skill case",
        )
        assert result.get("skills", []) == []
        assert "skill_prelude" not in result


# ---------------------------------------------------------------------------
# build_skill_prelude — strength-ordering + format
# ---------------------------------------------------------------------------

class TestBuildPrelude:
    def test_system_outranks_prelude_outranks_reminder(self):
        rows = [
            {"name": "r", "inject_as": "reminder", "prompt": "soft tip"},
            {"name": "s", "inject_as": "system",   "prompt": "hard rule"},
            {"name": "p", "inject_as": "prelude",  "prompt": "thinking mode"},
        ]
        out = sk.build_skill_prelude(rows)
        # System block should appear before prelude before reminder.
        s_idx = out.find("Methodology override")
        p_idx = out.find("Cognitive mode")
        r_idx = out.find("Skill active")
        assert -1 < s_idx < p_idx < r_idx, (s_idx, p_idx, r_idx)

    def test_match_via_renders_in_block(self):
        rows = [{"name": "x", "inject_as": "prelude", "prompt": "p",
                  "match_via": "attached"}]
        out = sk.build_skill_prelude(rows)
        assert "(match: attached)" in out
