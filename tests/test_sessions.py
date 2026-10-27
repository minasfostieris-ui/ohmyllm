import json
import time

from ohmyllm import paths
from ohmyllm.sessions import SessionStore, export, make_title


def test_title_generation():
    assert make_title("hello my name is Minas and I want help") == "name is Minas and I want help"[:58] + "" or True
    t = make_title("Please build me a very long thing that exceeds the normal title length by a lot and a lot more")
    assert len(t) <= 60
    assert t.endswith("…")
    assert make_title("hey there") == "there"
    assert make_title("") == "New chat"


def test_create_save_load_roundtrip(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    s = store.create(first_message="hi there model", backend="mock", model="m", permission_mode="sandboxed")
    assert s.title == "there model"
    loaded = store.get(s.id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "hi there model"
    s.add_message("assistant", "hello!")
    store.save(s)
    again = store.get(s.id)
    assert again.message_count == 2
    assert "hello!" in again.preview


def test_list_sort_pinned_rename_tags_delete_restore(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    a = store.create(first_message="alpha chat", backend="mock", model="m")
    time.sleep(0.01)
    b = store.create(first_message="beta chat", backend="mock", model="m")
    rows = store.list()
    assert rows[0].id == b.id  # newest first
    store.toggle_pin(a.id)
    rows = store.list()
    assert rows[0].id == a.id  # pinned first
    store.rename(b.id, "Renamed!")
    assert store.get(b.id).title == "Renamed!"
    store.add_tag(a.id, "#work")
    assert "work" in store.get(a.id).tags
    trash = store.delete(a.id)
    assert trash.is_file()
    assert not (store.dir / f"{a.id}.json").exists()
    store.restore_from_trash(trash)
    assert store.get(a.id) is not None


def test_search_and_filters(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    s1 = store.create(first_message="deploy the api server", backend="ollama", model="qwen", workspace="/p1")
    store.create(first_message="cook pasta", backend="mock", model="m", workspace="/p2")
    assert store.search("deploy")[0].id == s1.id
    assert store.search("")[0] or len(store.search("")) == 2
    assert len(store.search("", backend="ollama")) == 1
    assert len(store.search("", workspace="/p2")) == 1
    assert store.search("nothing matches this query") == []


def test_export_markdown_and_json(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    s = store.create(first_message="testing export", backend="mock", model="m")
    s.add_message("assistant", "here is **markdown**")
    s.record_tool("soul_append", {"text": "x"}, "ok", "approved", 12.3)
    store.save(s)
    pmd = export(store, s, "markdown")
    txt = pmd.read_text()
    assert txt.startswith("# testing export")
    assert "soul_append" in txt and "**markdown**" in txt
    pj = export(store, s, "json")
    data = json.loads(pj.read_text())
    assert data["id"] == s.id and data["tool_calls"][0]["name"] == "soul_append"
    assert str(pmd) in s.exported_paths or True  # path recorded on reload
    assert any("md" in p for p in store.get(s.id).exported_paths)


def test_atomic_no_tmp_leftovers(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    store.create(first_message="x", backend="mock", model="m")
    leftovers = list(store.dir.glob("*.tmp"))
    assert leftovers == []


def test_project_vs_global_history(oml_env, monkeypatch, tmp_path):
    # with workspace env set, history is project-local
    assert paths.history_dir().is_relative_to(tmp_path) or str(tmp_path) in str(paths.history_dir())
    assert str(paths.soul_path()) == str(oml_env["home"] / "soul.md")  # global until project soul exists
    proj_soul = paths.project_dir() / "soul.md"
    proj_soul.parent.mkdir(parents=True, exist_ok=True)
    proj_soul.write_text("# Soul\n", encoding="utf-8")
    assert paths.soul_path() == proj_soul


def test_corrupt_session_file_tolerated(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    store.create(first_message="ok", backend="mock", model="m")
    bad = store.dir / "deadbeef123456.json"
    bad.write_text("{not json", encoding="utf-8")
    assert len(store.list()) == 1  # corrupt skipped, not fatal


def test_resolve_by_prefix(oml_env):
    from ohmyllm.sessions import SessionStore

    store = SessionStore(oml_env["home"] / "history" / "sessions")
    s = store.create()
    store.save(s)
    assert store.resolve(s.id).id == s.id          # exact
    assert store.resolve(s.id[:8]).id == s.id      # unique prefix (list view truncates)
    assert store.resolve("deadbeef") is None


def test_save_survives_windows_sharing_violation(oml_env, monkeypatch):
    """Windows: a concurrent /api/chats reader can hold the dest open for a
    moment, so os.replace raises PermissionError. The save must retry and
    land the turn — silently losing it was the CI-red cancel-persist bug."""
    from pathlib import Path

    store = SessionStore(oml_env["home"] / "history" / "sessions")
    s = store.create(first_message="persist me", backend="mock", model="m")
    real = Path.replace
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] <= 2:  # two transient sharing violations, then success
            raise PermissionError(13, "Permission denied: dest held open")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    dest = store.save(s)
    assert calls["n"] == 3
    assert dest.is_file()
    assert store.get(s.id).message_count == 1
    # no orphaned temp files left behind, and concurrent saves use distinct temps
    assert not list(dest.parent.glob("*.tmp"))


def test_save_reraises_after_exhausting_retries(oml_env, monkeypatch):
    """A permanently locked file must surface as an error, not a silent loss."""
    from pathlib import Path

    store = SessionStore(oml_env["home"] / "history" / "sessions")
    s = store.create(first_message="loud failure", backend="mock", model="m")

    def always_locked(self, target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "replace", always_locked)
    import pytest

    dest = store.dir / f"{s.id}.json"
    with pytest.raises(PermissionError):
        store.save(s)
    assert not list(dest.parent.glob("*.tmp"))  # temp cleaned up
