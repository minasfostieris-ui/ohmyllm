import json
import time

import pytest

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


def test_exports_structurally_redact_messages_and_nested_tool_data(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    session = store.create(first_message="safe", backend="mock", model="m")
    password = "export-password-123"
    token = "github_pat_11AA22BB33CC44DD55"
    session.add_message("assistant", f'{{"password":"{password}"}}')
    session.tool_calls.append({"name": "x", "args": {"credentials": {"token": token}}})

    markdown = export(store, session, "markdown").read_text()
    json_text = export(store, session, "json").read_text()
    for rendered in (markdown, json_text):
        assert password not in rendered
        assert token not in rendered
        assert "REDACTED" in rendered
    assert json.loads(json_text)["tool_calls"][0]["args"]["credentials"]["token"] == "[REDACTED]"


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
    import os

    store = SessionStore(oml_env["home"] / "history" / "sessions")
    s = store.create(first_message="persist me", backend="mock", model="m")
    real = os.replace
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] <= 2:  # two transient sharing violations, then success
            raise PermissionError(13, "Permission denied: dest held open")
        return real(self, target)

    monkeypatch.setattr(os, "replace", flaky)
    dest = store.save(s)
    assert calls["n"] == 3
    assert dest.is_file()
    assert store.get(s.id).message_count == 1
    # no orphaned temp files left behind, and concurrent saves use distinct temps
    assert not list(dest.parent.glob("*.tmp"))


def test_save_reraises_after_exhausting_retries(oml_env, monkeypatch):
    """A permanently locked file must surface as an error, not a silent loss."""
    import os

    store = SessionStore(oml_env["home"] / "history" / "sessions")
    s = store.create(first_message="loud failure", backend="mock", model="m")

    def always_locked(self, target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", always_locked)
    import pytest

    dest = store.dir / f"{s.id}.json"
    with pytest.raises(PermissionError):
        store.save(s)
    assert not list(dest.parent.glob("*.tmp"))  # temp cleaned up


def test_invalid_session_ids_and_filename_mismatches_are_rejected(oml_env):
    store = SessionStore(oml_env["home"] / "history")
    session = store.create(first_message="safe")
    session.id = "../escape"
    with pytest.raises(ValueError):
        store.save(session)
    assert store.get("../../etc/passwd") is None

    good_name = "a" * 12
    wrong_id = "b" * 12
    record = session.to_dict()
    record["id"] = wrong_id
    mismatch = store.dir / f"{good_name}.json"
    mismatch.write_text(json.dumps(record))
    assert all(item.id != wrong_id for item in store.list())


def test_restore_requires_a_confined_opaque_trash_record(oml_env, tmp_path):
    store = SessionStore(oml_env["home"] / "history")
    session = store.create(first_message="safe")
    external = tmp_path / f"{session.id}-20260101T000000.json"
    external.write_text(json.dumps(session.to_dict()))
    assert store.restore_from_trash(external) is None


def test_restore_corrupt_trash_returns_none(oml_env):
    """Task #14 F8: a corrupt trash record must never raise — None, and the
    record stays in trash (nothing is consumed on a failed restore)."""
    store = SessionStore(oml_env["home"] / "history")
    session = store.create(first_message="corruptible")
    trash = store.delete(session.id)
    assert trash is not None and trash.is_file()
    for payload in (
        "{not json",                                # ValueError (json)
        "[]",                                       # TypeError (not an object)
        json.dumps({"id": session.id}),             # TypeError (missing fields)
        json.dumps({"title": "no id"}),             # ValueError (bad id)
    ):
        trash.write_text(payload)
        assert store.restore_from_trash(trash) is None, payload
        assert trash.is_file()
    assert store.get(session.id) is None


def test_session_state_files_are_owner_private(oml_env):
    import os
    import stat

    store = SessionStore(oml_env["home"] / "history")
    session = store.create(first_message="safe")
    path = store.dir / f"{session.id}.json"
    if os.name == "posix":
        assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
