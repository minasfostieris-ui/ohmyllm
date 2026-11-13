"""D9: skills level-2 — bundled reference files, strictly inside the skill dir."""

from __future__ import annotations

import pytest

from ohmyllm.skills import SkillManager

SKILL_MD = """---
name: demo
description: Demo skill for level-2 reference tests
---
# Demo
Main instructions live here.
"""


@pytest.fixture()
def mgr(tmp_path):
    root = tmp_path / "skills"
    skill = root / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD)
    (skill / "references" / "api.md").write_text("REFERENCE-BODY endpoint list")
    return SkillManager(root)


def test_level1_read_unchanged(mgr):
    body = mgr.read("demo")
    assert body is not None and "Main instructions live here." in body


def test_level2_reference_read(mgr):
    body = mgr.read("demo", path="references/api.md")
    assert body is not None and "REFERENCE-BODY" in body


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "../outside.md",
    "/etc/passwd",
    "references/./api.md",
    "references/../SKILL.md",
    "references\\api.md",
    "",
    ".",
    "..",
    "references/nope.md",
])
def test_unsafe_or_missing_paths_refused(mgr, bad, tmp_path):
    (tmp_path / "outside.md").write_text("OUTSIDE")
    with pytest.raises(ValueError):
        mgr.read("demo", path=bad)


def test_symlinked_reference_refused(mgr, tmp_path):
    target = tmp_path / "secret.md"
    target.write_text("SECRET")
    skill = mgr.root / "demo"
    (skill / "references" / "evil.md").symlink_to(target)
    with pytest.raises(ValueError):
        mgr.read("demo", path="references/evil.md")


def test_symlinked_directory_component_refused(mgr, tmp_path):
    outside = tmp_path / "outside-refs"
    outside.mkdir()
    (outside / "api.md").write_text("OUTSIDE")
    skill = mgr.root / "demo"
    (skill / "refs-link").symlink_to(outside)
    with pytest.raises(ValueError):
        mgr.read("demo", path="refs-link/api.md")


def test_reference_cap_and_truncation_marker(mgr):
    skill = mgr.root / "demo"
    (skill / "references" / "big.md").write_text("y" * 9000)
    body = mgr.read("demo", max_chars=100, path="references/big.md")
    assert body is not None
    assert "[truncated]" in body
    assert body.count("y") <= 100


def test_skill_md_cap_still_applies(mgr):
    body = mgr.read("demo", max_chars=10)
    assert body is not None and len(body) <= 10 + len("\n… [truncated]")
    assert "[truncated]" in body


def test_unknown_skill_returns_none(mgr):
    assert mgr.read("nope") is None
    assert mgr.read("nope", path="references/api.md") is None


def test_registry_skills_read_accepts_path(mgr):
    import asyncio

    from ohmyllm.config.models import Config
    from ohmyllm.tools.registry import build_default_registry
    from ohmyllm.tools.spec import ToolContext

    reg = build_default_registry()
    ctx = ToolContext(workspace=mgr.root, mode="sandboxed", config=Config(),
                      soul=None, skills=mgr)
    spec = next(s for s in reg.specs() if s.name == "skills_read")
    assert any(p.name == "path" for p in spec.params)
    res = asyncio.run(reg.execute("skills_read", {"name": "demo", "path": "references/api.md"}, ctx))
    assert res.ok and "REFERENCE-BODY" in res.output
    bad = asyncio.run(reg.execute("skills_read", {"name": "demo", "path": "../../etc/passwd"}, ctx))
    assert not bad.ok
