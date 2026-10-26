"""The thinking UX fixes: override shadowing, feed rotation, hide, autoscroll."""
from __future__ import annotations

import pytest
from textual.widgets import Collapsible, Markdown, Static


def test_model_override_cannot_shadow_thinking_toggles():
    from ohmyllm.config import BackendName, Config, GenerationSettings

    cfg = Config()
    cfg.generation.ultrathink = True
    cfg.generation.effort = "high"
    # a per-model override saved while ultrathink was OFF (the real-world case)
    ov = GenerationSettings(num_ctx=131072, ultrathink=False, thinking=False)
    cfg.set_model_settings(BackendName.LMSTUDIO, "qwen3.5", ov)
    eff = cfg.settings_for(BackendName.LMSTUDIO, "qwen3.5")
    assert eff.num_ctx == 131072, "real per-model knobs still apply"
    assert eff.ultrathink is True, "global /ultrathink must survive the override"
    assert eff.effort == "high", "global /think level must survive the override"
    assert eff.thinking is True


async def test_feed_thinking_line_rotates(oml_env, svc):
    from ohmyllm.tui import widgets
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.widgets import ToolPanel

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        panel = app.screen.query_one(ToolPanel)
        panel.add_event({"type": "thinking", "step": 2})
        first = panel._lines[-1].plain
        assert first.startswith("💭") and "(step 2)" in first
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(widgets, "verb", lambda *a, **k: "zzz-test-verb")
            panel.rotate_thinking()
        assert panel._lines[-1].plain == "💭 zzz-test-verb (step 2)"
        # non-thinking trailing lines are never touched
        panel.add_event({"type": "tool_result", "name": "read_file", "ok": True})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(widgets, "verb", lambda *a, **k: "nope")
            panel.rotate_thinking()
        assert "nope" not in panel._lines[-1].plain


async def test_hide_thinking_replaces_stream_with_count_line(oml_env, svc):
    from textual.containers import VerticalScroll

    from ohmyllm.tui.app import OhMyLLMApp

    svc.config.agent.thinking_visible = False
    svc.save_config()  # the app boots its own Services from disk
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        for _ in range(10):
            app.dispatch_turn_event({"type": "thinking_text", "text": "hmm " * 40})
            await pilot.pause(0.05)
        assert not scr.query(".msg-think"), "hidden mode must not stream reasoning"
        hist = scr.query_one("#history", VerticalScroll)
        hidden = [c for c in hist.children if isinstance(c, Static) and "hidden" in str(c.render())]
        assert hidden, "a compact ·hidden· line must replace the flood"
        # sealing still produces the collapsed block
        app.dispatch_turn_event({"type": "turn_end", "ok": True})
        await pilot.pause(0.3)
        blocks = scr.query(Collapsible)
        assert any(b.title == "thinking process" for b in blocks)


async def test_ctrl_t_toggles_visibility_and_stream(oml_env, svc):
    from ohmyllm.tui.app import OhMyLLMApp

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        assert app.svc.config.agent.thinking_visible is True
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        assert app.svc.config.agent.thinking_visible is False
        app.dispatch_turn_event({"type": "thinking_text", "text": "deep thought "})
        await pilot.pause(0.2)
        assert not app.screen.query(Markdown) or all(
            "msg-think" not in m.classes for m in app.screen.query(Markdown))
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        assert app.svc.config.agent.thinking_visible is True
        assert app.screen.query(".msg-think"), "show mode re-attaches the live stream"


async def test_autoscroll_respects_user_scroll_up(oml_env, svc):
    from textual.containers import VerticalScroll

    from ohmyllm.config import Config
    from ohmyllm.tui.app import OhMyLLMApp

    _ = Config  # keep import used marker
    app = OhMyLLMApp()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        hist = scr.query_one("#history", VerticalScroll)
        # flood content until the view actually scrolls
        for i in range(120):
            app.dispatch_turn_event({"type": "token", "text": f"\n\nline {i} padding padding padding\n\n"})
            await pilot.pause(0.07)
        await pilot.pause(0.2)
        assert hist.max_scroll_y > 0, "content must overflow for this test"
        hist.focus()
        for _ in range(15):
            await pilot.press("pageup")
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        before = hist.scroll_y
        assert before < hist.max_scroll_y - 2, "user scroll-up must have taken over"
        for i in range(30):
            app.dispatch_turn_event({"type": "token", "text": f"more {i}\n"})
            await pilot.pause(0.05)
        assert hist.scroll_y <= before + 1, "streaming must not yank a reading user back down"
        hist.scroll_end(animate=False)
        await pilot.pause(0.1)
        for i in range(30):
            app.dispatch_turn_event({"type": "token", "text": f"tail {i}\n"})
            await pilot.pause(0.05)
        assert hist.scroll_y >= hist.max_scroll_y - 2, "at-bottom users keep following"
