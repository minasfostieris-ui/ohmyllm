
import pytest

from ohmyllm.tui.app import OhMyLLMApp


async def _boot(app, pilot_pause=0.7):
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(pilot_pause)
        return app, pilot


@pytest.mark.asyncio()
async def test_tui_chat_memory_and_persist(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        from ohmyllm.tui.screens.chat import ChatScreen

        assert isinstance(app.screen, ChatScreen)
        inp = app.screen.query_one("#chat-input")
        inp.value = "hello my name is TuiMemory"
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.12)
            if not app.busy:
                break
        assert not app.busy
        assert "TuiMemory" in svc.soul.read()
        # message rendered in history
        texts = " ".join(str(getattr(w, "_content", "")) for w in app.screen.walk_children())
        assert "TuiMemory" in texts or "soul_append" in texts
        sid = app.session.id
    # persisted after shutdown
    from ohmyllm import paths
    from ohmyllm.sessions import SessionStore

    store = SessionStore(paths.history_dir())
    sess = store.get(sid)
    assert sess and any("TuiMemory" in str(m.get("content")) for m in sess.messages)


@pytest.mark.asyncio()
async def test_tui_file_preview_streams_past_tail_limit(oml_env, svc):
    """The live write preview must keep updating beyond the 900-char tail
    window (old bug: froze once the content marker slid out of the tail)
    and must render colored, not grey."""
    import json
    import re

    app = OhMyLLMApp()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.6)
        scr = app.screen
        content = "".join(f"def f{i}():\n    return {i}\n" for i in range(300))
        args_full = json.dumps({"path": "big.py", "content": content})
        cm = re.search(r'"content"\s*:\s*"', args_full)
        assert cm
        for k in range(60, len(args_full), 300):
            scr._file_flush = 0.0
            scr._file_preview({
                "type": "tool_stream", "name": "write_file", "chars": k,
                "tail": args_full[max(0, k - 900):k],
                "content_tail": args_full[cm.end():k][-1600:] if k > cm.end() else "",
            })
        await pilot.pause(0.3)
        body = scr._file_live._Static__content
        plain = body.plain
        assert "writing write_file" in plain
        assert "def f299" in plain or "return 29" in plain, \
            "preview froze before the stream end: " + plain[-60:]
        assert "def f0:" not in plain, "preview shows the start, not the live edge"
        styles = {str(sp.style) for sp in body.spans}
        assert "bold cyan" in styles and "bright_white" in styles, styles


@pytest.mark.asyncio()
async def test_tui_approval_modal(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        be = app.svc.backend()
        from ohmyllm.backends.base import ChatChunk
        call = {"id": "t1", "function": {"name": "write_file",
                "arguments": {"path": "gui-note.txt", "content": "written via TUI approval"}}}
        be.script = [
            [ChatChunk(content="", tool_calls=[call], done=True)],
            [ChatChunk(content="noted."), ChatChunk(done=True)],
        ]
        inp = app.screen.query_one("#chat-input")
        inp.value = "write a note file"
        await pilot.press("enter")
        modal_seen = False
        for _ in range(60):
            await pilot.pause(0.1)
            if "ApprovalModal" in type(app.screen).__name__:
                modal_seen = True
                await pilot.press("y")
                break
        assert modal_seen, "approval modal must appear for workspace writes"
        for _ in range(80):
            await pilot.pause(0.1)
            if not app.busy:
                break
        assert (svc.workspace / "gui-note.txt").read_text() == "written via TUI approval"


@pytest.mark.asyncio()
async def test_tui_chats_screen_and_draft_preserved(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "first message for browsing"
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.1)
            if not app.busy:
                break
        # unsent draft must survive screen switch
        app.screen.query_one("#chat-input").value = "draft text"
        app.replace_screen("chats")
        await pilot.pause(0.8)
        table = app.screen.query_one("#chats-table")
        assert table.row_count >= 1
        app.replace_screen("chat")
        await pilot.pause(0.5)
        assert app.screen.query_one("#chat-input").value == "draft text"


@pytest.mark.asyncio()
async def test_tui_palette_and_help(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.6)
        await pilot.press("ctrl+k")
        await pilot.pause(0.5)
        assert "Palette" in type(app.screen).__name__ or "Command" in type(app.screen).__name__
        await pilot.press("escape")
        await pilot.pause(0.4)
        await pilot.press("ctrl+h")
        await pilot.pause(0.5)
        assert type(app.screen).__name__ == "HelpScreen"


async def test_wizard_rapid_rerender_no_duplicate_ids(oml_env, svc):
    """Worker-complete re-render must not race the previous removal (DuplicateIds)."""
    from ohmyllm.tui.screens.wizard import WizardScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)
        wiz = app.screen if isinstance(app.screen, WizardScreen) else None
        if wiz is None:
            app.push_screen(WizardScreen())
            await pilot.pause(0.4)
            wiz = app.screen
        wiz.step = 1
        await wiz._show_step()
        await wiz._show_step()   # immediate re-render (the old crash path)
        await wiz._show_step()
        assert wiz.query_one("#wiz-backend")
        await pilot.pause(0.3)


async def test_wizard_models_step_populates_list(oml_env, svc):
    from textual.widgets import OptionList

    from ohmyllm.tui.screens.wizard import WizardScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)
        if not isinstance(app.screen, WizardScreen):
            app.push_screen(WizardScreen())
            await pilot.pause(0.4)
        wiz = app.screen
        wiz.chosen_backend = "mock"
        wiz.step = 3
        await wiz._show_step()
        await pilot.pause(1.2)  # _load_models worker lands
        ol = wiz.query_one("#wiz-models", OptionList)
        assert ol.option_count >= 1, "model list empty after worker — replace() regression"
        assert wiz.chosen_model, "no default model chosen"


@pytest.mark.asyncio()
async def test_chats_row_selection_and_open(oml_env, svc):
    s1 = svc.new_session("alpha chat")
    s1.add_message("user", "hi from alpha")
    svc.sessions.save(s1)
    s2 = svc.new_session("beta chat")
    svc.sessions.save(s2)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.replace_screen("chats")
        await pilot.pause(1.0)
        sc = app.screen
        assert sc.query_one("#chats-table").row_count >= 2
        sid = sc._selected_id()
        assert sid in (s1.id, s2.id, app.session.id), "cursor row must resolve to a session id"
        await pilot.pause(0.7)
        prev = sc.query_one("#chats-preview")
        assert "no chats" not in str(prev.get_option_at_index(0).prompt)
        await pilot.press("enter")
        await pilot.pause(0.8)
        from ohmyllm.tui.screens.chat import ChatScreen

        assert isinstance(app.screen, ChatScreen)
        assert app.session is not None and app.session.id == sid


@pytest.mark.asyncio()
async def test_turn_stats_footer_and_panel_hidden(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        from ohmyllm.tui.widgets import ToolPanel

        panel = app.screen.query_one(ToolPanel)
        assert panel.display is False, "tool panel must not claim width when idle"
        inp = app.screen.query_one("#chat-input")
        inp.value = "hello stats"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.12)
            if not app.busy:
                break
        assert not app.busy
        stats = [w for w in app.screen.walk_children() if "turn-stats" in (w.classes or set())]
        assert stats, "per-turn stats line missing after reply"
        line = str(stats[-1].render())
        assert "step" in line or "tok" in line, line


@pytest.mark.asyncio()
async def test_reply_rendered_exactly_once(oml_env, svc):
    from textual.widgets import Markdown

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "plain question with no memory content"
        await pilot.press("enter")
        for _ in range(120):
            await pilot.pause(0.12)
            if not app.busy:
                break
        assert not app.busy
        sources = [w.source for w in app.screen.walk_children() if isinstance(w, Markdown) and w.source.strip()]
        assert sources, "assistant reply never rendered"
        assert len(sources) == len(set(sources)), f"reply rendered twice: {sources!r}"


@pytest.mark.asyncio()
async def test_wizard_cannot_grant_full_access(oml_env, svc):
    from textual.widgets import OptionList

    from ohmyllm.config import PermissionMode
    from ohmyllm.tui.screens.wizard import WizardScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        if not isinstance(app.screen, WizardScreen):
            app.push_screen(WizardScreen())
            await pilot.pause(0.4)
        wiz = app.screen
        wiz.step = 6
        await wiz._show_step()
        await pilot.pause(0.3)
        ol = wiz.query_one("#wiz-mode", OptionList)
        assert ol.option_count == 4, "full_access must not be offered in the wizard"
        ol.index = 3
        assert wiz._mode_choice is not PermissionMode.FULL_ACCESS


@pytest.mark.asyncio()
async def test_think_toggle_persists(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/think on"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert app.svc.config.generation.effort == "medium"
        inp.value = "/think high"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert app.svc.config.generation.effort == "high"
        inp.value = "/think"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert app.svc.config.generation.effort == "xhigh", "bare /think cycles the ladder"
        inp.value = "/think off"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert app.svc.config.generation.thinking is False


@pytest.mark.asyncio()
async def test_turn_finishing_on_detached_screen_no_crash(oml_env, svc):
    """User crash: switched screens mid-turn; the worker's post-turn UI calls
    hit a detached ChatScreen and raised NoMatches."""
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        old = app.screen
        app.replace_screen("chats")
        await pilot.pause(0.5)
        from textual.css.query import NoMatches

        try:  # precondition: children are gone (the real crash condition)
            old.query_one("#chat-title")
            raise AssertionError("expected detached screen to have no #chat-title")
        except NoMatches:
            pass
        old._refresh_title()
        old._sysline("note", "system")
        old._stats_line({"steps": 2, "tokens_per_s": 5.0, "ttft_s": 1.0, "calls": [], "usage": {}})
        old._finalize("some text")
        old._on_event({"type": "token", "text": "x"})
        old._push_status()
        assert isinstance(app.screen, type(app.screen))  # app alive, no raise


@pytest.mark.asyncio()
async def test_quit_confirm_from_binding_context_no_crash(oml_env, svc):

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.action_safe_quit()  # sync binding: spawns its own worker, pump stays free
        await pilot.pause(0.5)
        assert "ConfirmModal" in type(app.screen).__name__
        await pilot.press("n")
        await pilot.pause(0.4)
        assert app.is_running


@pytest.mark.asyncio()
async def test_slash_menu_pops_and_completes(oml_env, svc):
    from textual.widgets import OptionList

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/ren"
        inp.action_end()
        await pilot.pause(0.3)
        menu = app.screen.query_one("#slash-menu", OptionList)
        assert menu.display and menu.option_count == 1
        await pilot.press("enter")
        await pilot.pause(0.3)
        # unambiguous prefix: Enter RUNS the command — input cleared, and
        # crucially nothing reaches the model as a chat message
        assert app.screen.query_one("#chat-input").value == ""
        assert app.busy is False
        assert app.session is None or app.session.message_count == 0
        # ambiguous prefix: Enter only completes (several /mo* commands)
        inp.value = "/mo"
        inp.action_end()
        await pilot.pause(0.3)
        assert menu.option_count > 1
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.screen.query_one("#chat-input").value.startswith("/")
        assert app.busy is False


@pytest.mark.asyncio()
async def test_thinking_block_expandable(oml_env, svc):
    from textual.widgets import Collapsible

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        scr._on_event({"type": "thinking_text", "text": "step by step reasoning here"})
        scr._on_event({"type": "turn_end", "text": "answer", "steps": 1, "calls": [], "tokens_per_s": 5.0})
        await pilot.pause(0.4)
        cols = list(scr.walk_children(Collapsible))
        assert cols, "no thinking collapsible mounted"
        assert "thinking" in str(cols[-1].title).lower()
        assert cols[-1].collapsed, "should start collapsed (expand to read)"


@pytest.mark.asyncio()
async def test_gui_slash_launches_bridge(oml_env, svc, monkeypatch):
    import pathlib

    calls = {}

    def fake_popen(args, **kw):
        calls["args"] = list(args)
        if "--port-file" in args:
            pf = pathlib.Path(args[args.index("--port-file") + 1])
            pf.write_text("54321 tok123")

        class P:
            pid = 999999

        return P()

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: calls.setdefault("url", url))

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/gui"
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.1)
            if "args" in calls and "url" in calls:
                break
        args = calls.get("args", [])
        bridge = "ohmyllm.bridge" in args
        assert bridge, f"web bridge never launched: {args}"
        # the TUI surfaces the URL via ~/.ohmyllm/gui-web.url (notify + file)
        import ohmyllm.paths as P
        got = (P.global_home() / "gui-web.url")
        assert got.is_file() and got.read_text().startswith("http://127.0.0.1:54321/?t="), "url file missing"


@pytest.mark.asyncio()
async def test_thinking_streams_live_then_seals(oml_env, svc):
    from textual.widgets import Collapsible

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        scr._on_event({"type": "thinking_text", "text": "ponder one "})
        await pilot.pause(0.2)
        lives = [w for w in scr.walk_children() if "msg-think" in (w.classes or set())]
        assert lives, "no live thinking widget while reasoning streams"
        scr._on_event({"type": "thinking_text", "text": "ponder two"})
        scr._on_event({"type": "token", "text": "the answer"})
        await pilot.pause(0.2)
        cols = [w for w in scr.walk_children() if isinstance(w, Collapsible)]
        assert cols, "sealed thinking collapsible missing"
        sealed = [c for c in cols if c.title == "thinking process"]
        assert sealed, "the live block must seal IN PLACE, not vanish"
        assert all(c.collapsed for c in sealed), "sealed thinking must be collapsed once the answer starts"


@pytest.mark.asyncio()
async def test_slash_menu_visible_above_input(oml_env, svc):
    from textual.widgets import OptionList

    app = OhMyLLMApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(0.8)
        inp = app.screen.query_one("#chat-input")
        for ch in "/cha":
            await pilot.press(ch)
            await pilot.pause(0.05)
        menu = app.screen.query_one("#slash-menu", OptionList)
        assert menu.display and menu.option_count >= 1
        # every match must get a row (capped by max-height 10); the old >= 3
        # only passed because a glyph border padded two phantom rows
        assert menu.region.height >= min(menu.option_count, 10), f"menu clipped: {menu.region}"
        assert menu.region.y < inp.region.y, "menu must sit above the input, not behind it"


@pytest.mark.asyncio()
async def test_file_edit_line_in_history(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        scr._on_event({"type": "tool_result", "name": "write_file", "ok": True,
                       "output": "wrote", "file": {"path": "index.html", "created": True,
                                                   "added": 42, "removed": 0, "changed": 0}})
        await pilot.pause(0.3)
        files = [w for w in scr.walk_children() if "msg-file" in (w.classes or set())]
        assert files, "no 📄 created line mounted"
        txt = str(files[-1].render())
        assert "created" in txt and "index.html" in txt and "+42" in txt


@pytest.mark.asyncio()
async def test_busy_send_queues_instead_of_refusing(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        app.busy = True
        try:
            scr._send("hold this thought")
            await pilot.pause(0.3)
        finally:
            app.busy = False
        assert app.interjections == ["hold this thought"]
        texts = [str(w.render()) for w in scr.walk_children() if "queued" in str(getattr(w, "_renderable", w.render() if hasattr(w, "render") else "")).lower()]
        assert texts, "no queued indicator shown"


@pytest.mark.asyncio()
async def test_live_file_preview_mounts_and_seals(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        scr._on_event({"type": "tool_stream", "name": "write_file", "chars": 300,
                       "tail": '{"path": "x.html", "content": "<html>\\n<h1>hi</h1>'})
        await pilot.pause(0.3)
        live = [w for w in scr.walk_children() if "msg-file-live" in (w.classes or set())]
        assert live, "no live file preview while streaming args"
        assert "hi" in str(live[0].render())
        scr._on_event({"type": "tool_result", "name": "write_file", "ok": True, "output": "wrote",
                       "file": {"path": "x.html", "created": True, "added": 2, "removed": 0, "changed": 0}})
        await pilot.pause(0.3)
        assert not [w for w in scr.walk_children() if "msg-file-live" in (w.classes or set())], \
            "live preview must be removed when the write completes"
        assert [w for w in scr.walk_children() if "msg-file" in (w.classes or set())]


@pytest.mark.asyncio()
async def test_quit_modal_single_instance_and_cancel(oml_env, svc):
    # action_safe_quit is sync-by-design: it spawns a worker so the message
    # pump stays free (a modal awaited inside a binding froze the whole CLI)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        await pilot.press("ctrl+q")
        await pilot.pause(0.4)
        await pilot.press("ctrl+q")  # impatient double Ctrl+Q
        await pilot.pause(0.4)
        assert len(app.screen_stack) == 3, "second modal must not stack"
        await pilot.click("#no")
        await pilot.pause(0.4)
        assert app.is_running and "ConfirmModal" not in type(app.screen).__name__


@pytest.mark.asyncio()
async def test_delete_chat_completes(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.8)
        s = app.svc.new_session("doomed")  # newest → first row under the cursor
        s.add_message("user", "bye")
        app.svc.sessions.save(s)
        sid = s.id
        app.replace_screen("chats")
        await pilot.pause(1.0)
        await pilot.press("x")
        await pilot.pause(0.5)
        assert "ConfirmModal" in type(app.screen).__name__
        await pilot.click("#yes")
        for _ in range(40):
            await pilot.pause(0.2)
            if app.svc.sessions.get(sid) is None:
                break
        assert app.svc.sessions.get(sid) is None, "delete never completed"
        assert app.is_running
        await pilot.pause(0.8)  # interval + refresh must not crash the app
        assert "ConfirmModal" not in type(app.screen).__name__


FRAME_GLYPHS = set("│─┌┐├┤┬┴┼╭╮╰╯═║▁▂▃▄▅▆▇█▏▕▔▖▗▘▙▚▛▜▝▞▟")


def _segments(app):
    for s in app.screen._compositor.render_strips():
        yield from s._segments


def _truecolor(monkeypatch):
    """The compositor reduces colors to the terminal's color system; pin a
    truecolor TERM so ring-color assertions are exact."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


@pytest.mark.asyncio()
async def test_frames_painted_solid_no_glyph_borders(oml_env, svc, monkeypatch):
    """User P0: dashed glyph frames and grey alpha washes in their terminal.

    The chat screen chrome must paint its ring color as solid background
    cells and contain ZERO box-drawing/block glyphs anywhere (font breaks
    them into dashes/tofu)."""
    _truecolor(monkeypatch)
    app = OhMyLLMApp()
    async with app.run_test(size=(106, 34)) as pilot:
        await pilot.pause(0.8)
        ring = app.get_css_variables()["ring"].lower()
        segs = list(_segments(app))
        # no frame glyphs anywhere on a fresh chat screen
        assert not [seg for seg in segs if set(seg.text) & FRAME_GLYPHS]
        # the ask-anything box is ringed: ring-colored cells on all four sides
        inp = app.screen.query_one("#chat-input")
        band = app.screen.query_one("#chat-input-band")
        assert band.region.height == inp.region.height + 2  # 1-cell padding top+bottom
        top = segs and any(
            seg.style and seg.style.bgcolor and (seg.style.bgcolor.name or "").lower() == ring
            for seg in app.screen._compositor.render_strips()[band.region.y]._segments
        )
        assert top, "chat input top frame row is not painted in the ring color"
        # bottom frame row exists (the old dock bug clipped it)
        bottom_row = band.region.y + band.region.height - 1
        assert any(
            seg.style and seg.style.bgcolor and (seg.style.bgcolor.name or "").lower() == ring
            for seg in app.screen._compositor.render_strips()[bottom_row]._segments
        ), "chat input bottom frame row missing (clipped)"


@pytest.mark.asyncio()
async def test_models_scrollbar_continuous_ring_thumb(oml_env, svc, monkeypatch):
    """The restored side bar: thumb cells are blank (no fractional block
    glyphs) carrying the ring color — straight in every font."""
    from textual.widgets import DataTable

    _truecolor(monkeypatch)

    app = OhMyLLMApp()
    async with app.run_test(size=(106, 34)) as pilot:
        await pilot.pause(0.8)
        app.replace_screen("models")
        await pilot.pause(1.0)
        t = app.screen.query_one(DataTable)
        for i in range(40):
            t.add_row(f"filler{i}", "", "", "", "", "", "", "")
        await pilot.pause(0.5)
        assert t.show_vertical_scrollbar, "models table must keep its side bar"
        ring = app.get_css_variables()["ring"].lower()
        thumb = [
            seg
            for seg in _segments(app)
            if seg.style and seg.style.reverse and seg.style.color
            and (seg.style.color.name or "").lower() == ring
        ]
        assert thumb, "no ring-colored scrollbar thumb rendered"
        assert all(set(seg.text) <= {" "} for seg in thumb), "thumb uses glyphs, not blank cells"
