
from pathlib import Path

import pytest

from ohmyllm.memory import SOUL_TEMPLATE, SoulError, SoulManager, SoulSecretError


@pytest.fixture()
def soul(oml_env):
    return SoulManager(oml_env["home"] / "soul.md", oml_env["home"] / "backups" / "soul")


def test_ensure_creates_template(soul):
    soul.ensure()
    assert soul.exists()
    text = soul.read()
    for sec in ("## User", "## Projects", "## Preferences", "## Pets", "## Lessons", "## Important Notes"):
        assert sec in text


def test_append_and_sections(soul):
    soul.ensure()
    res = soul.append("User", "User's name is Minas.")
    assert res["action"] == "appended"
    assert "Minas" in soul.read()
    assert soul.entries("User")[0].body == "User's name is Minas."
    res2 = soul.append("user", "User's name is Minas.")
    assert res2["action"] == "duplicate"
    # new section auto-created
    soul.append("Hobbies", "likes chess")
    assert soul.entries("Hobbies")


def test_upsert_updates_similar(soul):
    soul.ensure()
    soul.append("Projects", "User is working on a CLI project called OhMyLLM.")
    res = soul.upsert("Projects", "User is working on a CLI platform called OhMyLLM.")
    assert res["action"] == "updated"
    assert "platform" in soul.read()
    assert soul.read().count("OhMyLLM.") == 1
    entries = soul.entries("Projects")
    assert len(entries) == 1


def test_search_and_forget(soul):
    soul.ensure()
    soul.append("Pets", "User has a pet rabbit named Bnuy.")
    hits = soul.search("rabbit")
    assert len(hits) == 1
    res = soul.forget("rabbit named Bnuy")
    assert res["action"] == "forgotten"
    assert "Bnuy" not in soul.read()
    assert soul.forget("nonexistent unicorn thing")["action"] == "not_found"


def test_forget_by_index(soul):
    soul.ensure()
    soul.append("User", "first fact")
    soul.append("User", "second fact")
    res = soul.forget(section="User", index=1)
    assert "first fact" in res["removed"]
    assert "second fact" in soul.read()


def test_backups_and_revert(soul):
    soul.ensure()
    soul.append("User", "alpha")
    soul.append("User", "beta")
    soul.revert()
    assert "beta" not in soul.read()
    soul.clear()
    assert soul.read().strip() == SOUL_TEMPLATE.strip()


def test_secret_refused(soul):
    soul.ensure()
    with pytest.raises(SoulSecretError):
        soul.append("Important Notes", "password=SuperSecret123!")
    soul.append("Important Notes", "api_key=sk-abcdefghijklmnop", force=True)
    assert "REDACTED" not in soul.read()  # storage keeps user-confirmed text verbatim
    assert "sk-abcdefghijklmnop" in soul.read()


def test_injection_truncated(soul):
    soul.ensure()
    for i in range(400):
        soul.append("Lessons", f"lesson number {i} with a somewhat long filler sentence body")
    block = soul.for_injection(max_chars=1000)
    assert len(block) <= 1040
    assert "truncated" in block


def test_empty_soul_not_injected(soul):
    soul.ensure()
    assert soul.for_injection() == ""


def test_changelog_records(soul):
    soul.ensure()
    soul.append("User", "gamma")
    rec = soul.recent(1)[0]
    assert rec["action"] == "append"
    assert "gamma" in rec["text"]


def test_recent_reads_only_a_bounded_changelog_tail(soul, monkeypatch):
    soul.changelog.write_bytes(b"discard\n" * 40_000 + b'{"action":"tail"}\n')
    original_read_bytes = Path.read_bytes

    def reject_unbounded_read(path):
        if path == soul.changelog:
            raise AssertionError("recent() read the complete changelog")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_unbounded_read)
    assert soul.recent(1) == [{"action": "tail"}]


def test_backup_written_before_each_edit(soul):
    soul.ensure()
    soul.append("User", "x")
    assert len(soul.backups()) >= 1


def test_backup_ids_are_confined_and_symlinks_are_rejected(soul, tmp_path):
    soul.ensure()
    soul.append("User", "safe fact")
    backup = soul.backups()[0]
    outside = tmp_path / backup.name
    outside.write_text("attacker controlled")

    with pytest.raises(SoulError):
        soul.revert(str(outside))
    backup.unlink()
    backup.symlink_to(outside)
    assert soul.backups() == []
    with pytest.raises(SoulError):
        soul.revert(backup.name)


def test_soul_state_is_private_and_zero_recent_is_empty(soul):
    import os
    import stat

    soul.ensure()
    soul.append("User", "private fact")
    assert soul.recent(0) == []
    if os.name == "posix":
        assert stat.S_IMODE(soul.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(soul.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(soul.changelog.stat().st_mode) == 0o600
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in soul.backups())


def test_edit_via_write_is_backed_up_atomic_and_private(soul):
    import os
    import stat

    soul.ensure()
    soul.append("Preferences", "prefers tabs over spaces")
    text = soul.read()
    assert "- " in text and "## Preferences" in text
    before = len(soul.backups())

    # Whole-file UI edits must use the manager's safe write path.
    soul.write("nonsense without sections")
    assert soul.entries() == []
    assert len(soul.backups()) == before + 1
    if os.name == "posix":
        assert stat.S_IMODE(soul.path.stat().st_mode) == 0o600
    with pytest.raises(SoulError):
        soul.revert("no-such-file.md")
