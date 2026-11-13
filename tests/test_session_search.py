"""D2: session FTS5 search — ranking, redaction, trash exclusion, bridge route."""

from __future__ import annotations

import json
import stat

import pytest

from ohmyllm import paths
from ohmyllm.sessions import SessionStore


@pytest.fixture()
def store(oml_env):
    return SessionStore(paths.history_dir())


def _mk(store, title, body, **kw):
    sess = store.create(title=title, first_message=body, backend="mock",
                        model="mock-tool-7b", workspace=str(kw.get("ws", "")))
    sess.add_message("user", body)
    store.save(sess)
    return sess


def test_hits_ranking_and_shape(store):
    dense = _mk(store, "deploy runbook", "deploy deploy deploy the service")
    sparse = _mk(store, "misc notes", "we might deploy someday")
    hits = store.search("deploy", 10)
    assert [h["session_id"] for h in hits] == [dense.id, sparse.id]
    assert set(hits[0]) == {"session_id", "title", "updated_iso", "snippet", "rank"}
    assert isinstance(hits[0]["rank"], float)
    assert hits[0]["rank"] <= hits[1]["rank"]  # bm25: lower is better
    assert "[" in hits[0]["snippet"]  # snippet markers


def test_tool_metadata_indexed_and_secrets_redacted(store):
    sess = _mk(store, "ops chat", "run the deploy task")
    sess.record_tool("run_command", {"command": "deploy now", "api_key": "sk-LIVESECRETKEY123456"},
                     "ok", "user", 12.0)
    store.save(sess)
    hits = store.search("deploy", 10)
    blob = json.dumps(hits)
    assert any(h["session_id"] == sess.id for h in hits)
    assert "sk-LIVESECRETKEY123456" not in blob
    assert "LIVESECRETKEY" not in blob


def test_message_secrets_redacted_at_index_time(store):
    sess = _mk(store, "leaky chat", "my key is sk-ANOTHERSECRET999999 and we deploy")
    hits = store.search("deploy", 10)
    assert any(h["session_id"] == sess.id for h in hits)
    assert "sk-ANOTHERSECRET999999" not in json.dumps(hits)


def test_deleted_and_trash_excluded(store):
    sess = _mk(store, "gone chat", "deploy the thing")
    assert store.search("deploy", 10)
    store.delete(sess.id)
    assert store.search("deploy", 10) == []
    assert any(store.trash.glob(f"{sess.id}-*.json"))


def test_rename_updates_index(store):
    sess = _mk(store, "old title", "some body text")
    store.rename(sess.id, "zebra migration")
    hits = store.search("zebra", 10)
    assert [h["session_id"] for h in hits] == [sess.id]
    assert hits[0]["title"] == "zebra migration"


def test_legacy_search_signature_preserved(store):
    sess = _mk(store, "legacy chat", "deploy legacy")
    rows = store.search("deploy")
    assert rows and rows[0].id == sess.id  # Session objects, not dicts
    assert store.search("", backend="mock")
    assert store.search("", backend="ollama") == []


def test_fts_injection_is_inert(store):
    _mk(store, "safe chat", "deploy safely")
    for q in ('" OR deploy --', 'deploy"', "*", "NEAR(a b)"):
        assert isinstance(store.search(q, 10), list)


def test_empty_query_and_limit_caps(store):
    for i in range(3):
        _mk(store, f"chat {i}", "deploy marker")
    assert store.search("", 10) == []
    assert len(store.search("deploy", 2)) == 2
    assert len(store.search("deploy", 999)) <= 50


def test_index_file_is_private(store, oml_env):
    _mk(store, "perm chat", "deploy private")
    found = list(oml_env["home"].rglob("search-index.sqlite3"))
    found += list(oml_env["ws"].rglob("search-index.sqlite3"))
    assert found, "index file missing"
    assert stat.S_IMODE(found[0].stat().st_mode) == 0o600


# ------------------------------------------------------------------ bridge
def test_bridge_search_route(bridge, oml_env):
    store = SessionStore(paths.history_dir())
    sess = _mk(store, "bridge chat", "deploy via bridge")
    out = bridge.get("/api/search?q=deploy&limit=5")
    assert any(h["session_id"] == sess.id for h in out["results"])
    assert set(out["results"][0]) == {"session_id", "title", "updated_iso", "snippet", "rank"}


def test_bridge_search_validation_and_auth(bridge, oml_env):
    _mk(SessionStore(paths.history_dir()), "bridge chat", "deploy again")
    assert bridge.get("/api/search?q=deploy", auth=False)["__status__"] == 401
    assert bridge.get("/api/search?q=" + "x" * 513)["__status__"] == 400
    assert bridge.get("/api/search?q=deploy&limit=51")["__status__"] == 400
    assert bridge.get("/api/search?q=deploy&limit=abc")["__status__"] == 400
    assert bridge.get("/api/search?q=deploy&limit=0")["__status__"] == 400


# --------------------------------------------------------------------- TUI
@pytest.mark.asyncio()
async def test_tui_search_command(oml_env, svc):
    from ohmyllm.tui.app import OhMyLLMApp

    _mk(SessionStore(paths.history_dir()), "tui findable", "zebraunique marker")
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/search zebraunique"
        await pilot.press("enter")
        await pilot.pause(0.5)
        texts = " ".join(str(w.render()) for w in app.screen.walk_children())
        assert "tui findable" in texts
