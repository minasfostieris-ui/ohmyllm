import textwrap

import pytest

from ohmyllm.skills import SkillManager

SKILL_A = textwrap.dedent("""\
    ---
    name: frontend
    description: Frontend standards for web work.
    ---
    # Frontend
    Do great things.
    """)
SKILL_B = textwrap.dedent("""\
    ---
    name: rust
    description: Rust conventions.
    ---
    # Rust
    unsafe is forever.
    """)


@pytest.fixture()
def src_skills(tmp_path):
    root = tmp_path / "opencode-skills"
    (root / "frontend").mkdir(parents=True)
    (root / "frontend" / "SKILL.md").write_text(SKILL_A)
    (root / "rust").mkdir()
    (root / "rust" / "SKILL.md").write_text(SKILL_B)
    (root / "not-a-skill").mkdir()
    return root


def test_import_parent_and_list(tmp_path, src_skills):
    mgr = SkillManager(tmp_path / "home" / "skills")
    names = mgr.import_path(src_skills)
    assert sorted(names) == ["frontend", "rust"]
    rows = mgr.list()
    assert {r.name for r in rows} == {"frontend", "rust"}
    assert rows[0].description


def test_import_single_skill_dir_and_refresh(tmp_path, src_skills):
    mgr = SkillManager(tmp_path / "s")
    assert mgr.import_path(src_skills / "frontend") == ["frontend"]
    # re-import refreshes, does not duplicate
    mgr.import_path(src_skills / "frontend")
    assert len(mgr.list()) == 1


def test_import_requires_skill_md(tmp_path):
    mgr = SkillManager(tmp_path / "s")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        mgr.import_path(tmp_path / "empty")


def test_tools_list_and_read(oml_env, svc):
    from ohmyllm.tools.spec import ToolContext

    svc.skills.import_path(__import__("pathlib").Path(__file__).parent / "fixtures" / "skills")
    ctx = ToolContext(workspace=svc.workspace, mode="sandboxed", config=svc.config,
                      soul=svc.soul, skills=svc.skills)
    import asyncio

    out = asyncio.run(svc.registry.execute("skills_list", {}, ctx))
    assert out.ok and "test-fixture" in out.output
    out2 = asyncio.run(svc.registry.execute("skills_read", {"name": "test-fixture"}, ctx))
    assert out2.ok and "fixture instructions" in out2.output
    out3 = asyncio.run(svc.registry.execute("skills_read", {"name": "nope"}, ctx))
    assert not out3.ok


def test_prompt_injects_skill_index(oml_env, svc):
    import pathlib

    svc.skills.import_path(pathlib.Path(__file__).parent / "fixtures" / "skills")
    rt = svc.runtime()
    sp = rt.prompt_builder.system_prompt(mode=svc.config.permission_mode, native=True)
    assert "## Skills" in sp and "test-fixture" in sp


def test_policy_skills_are_status(oml_env, svc):
    d = svc.policy.evaluate("skills_list", {})
    assert d.allowed and not d.requires_approval
    d = svc.policy.evaluate("skills_read", {"name": "x"})
    assert d.allowed and not d.requires_approval
