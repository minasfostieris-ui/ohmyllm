"""Regressions for the release's TUI quality-of-life behaviors.

Covers: follow-scroll + latest-activity button (Ctrl+End), draft recovery
after a failed submission, literal Rich-markup rendering of user text and
chat titles, and the compact-chat layout class at small terminal sizes.

Targets only stable widget IDs/classes (#jump-latest, #history, #chat-input,
#chat-title, .msg-user, .compact-chat) — chat.py is owned elsewhere and needs
no edits for these tests. All runs are headless Textual pilots on the mock
backend: no subprocesses, no network beyond the app itself.
"""
from __future__ import annotations

import pytest

from ohmyllm.tui.app import OhMyLLMApp


async def _wait_idle(app, pilot, tries: int = 120) -> None:
    for _ in range(tries):
        await pilot.pause(0.1)
        if not app.busy:
            return
    raise AssertionError("turn never finished")


@pytest.mark.asyncio()
async def test_latest_activity_button_and_ctrl_end(oml_env, svc):
    """Scrolling up must not be yanked back by new output; instead the
    latest-activity button appears, and Ctrl+End jumps back to the bottom."""
    from textual.containers import VerticalScroll
    from textual.widgets import Button, Static

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        hist = scr.query_one("#history", VerticalScroll)
        for i in range(80):
            hist.mount(Static(f"history line {i}"))
        await pilot.pause(0.3)
        assert hist.max_scroll_y > 0, "history must be scrollable for this test"
        btn = scr.query_one("#jump-latest", Button)
        assert btn.display is False  # hidden while idle/at the bottom
        # user scrolls up to read older output
        hist.scroll_to(y=0, animate=False)
        await pilot.pause(0.2)
        # new activity arrives while the user is scrolled up
        scr._on_event({"type": "token", "text": "fresh activity while scrolled up"})
        await pilot.pause(0.3)
        assert btn.display is True, "latest-activity button must appear for unseen output"
        assert hist.scroll_y < hist.max_scroll_y - 2, "scroll position must not be yanked"
        # Ctrl+End jumps to the latest activity and hides the button again
        await pilot.press("ctrl+end")
        await pilot.pause(0.3)
        assert btn.display is False
        assert hist.scroll_y >= hist.max_scroll_y - 2


@pytest.mark.asyncio()
async def test_failed_submission_restores_input(oml_env, svc, monkeypatch):
    """A backend failure must hand the draft back to the input, not eat it."""
    from ohmyllm.backends.base import BackendError

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        be = app.svc.backend()

        def broken_stream(*args, **kwargs):
            raise BackendError("synthetic backend outage")

        monkeypatch.setattr(be, "stream_chat", broken_stream)
        inp = app.screen.query_one("#chat-input")
        inp.value = "precious draft that must survive"
        await pilot.press("enter")
        await _wait_idle(app, pilot)
        assert app.screen.query_one("#chat-input").value == "precious draft that must survive"
        # the failure is surfaced, not silent
        texts = " ".join(str(w.render()) for w in app.screen.walk_children())
        assert "backend error" in texts.lower()


@pytest.mark.asyncio()
async def test_user_markup_rendered_literally(oml_env, svc):
    """User text containing Rich markup must render as typed — both on the
    live mount and after a history re-render (screen round-trip)."""
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "literal [bold]x[/bold] please"
        await pilot.press("enter")
        await _wait_idle(app, pilot)

        def user_lines():
            return [w for w in app.screen.walk_children() if "msg-user" in (w.classes or set())]

        users = user_lines()
        assert users, "user message never mounted"
        assert "[bold]x[/bold]" in str(users[-1].render()), "markup was interpreted, not escaped"
        # history re-render path (leaving and returning rebuilds the transcript)
        app.replace_screen("chats")
        await pilot.pause(0.5)
        app.replace_screen("chat")
        await pilot.pause(0.5)
        users = user_lines()
        assert users, "user message missing after history re-render"
        assert "[bold]x[/bold]" in str(users[-1].render())


@pytest.mark.asyncio()
async def test_chat_title_markup_escaped(oml_env, svc):
    """A chat title containing Rich markup must render literally in the
    title bar instead of crashing the screen or eating the brackets."""
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/rename [red]boom[/red] title"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert app.is_running
        title = app.screen.query_one("#chat-title")
        assert "[red]boom[/red]" in str(title.render()), "title markup was not escaped"


@pytest.mark.asyncio()
async def test_compact_chat_class_at_small_sizes(oml_env, svc):
    """Below 100 columns the chat screen carries .compact-chat (tool dock
    hidden, tighter bands); at 100+ it must be gone again."""
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        assert not scr.has_class("compact-chat")
        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.4)
        assert scr.has_class("compact-chat"), "80x24 must switch to the compact layout"
        await pilot.resize_terminal(100, 30)
        await pilot.pause(0.4)
        assert not scr.has_class("compact-chat"), "threshold is width < 100"
        await pilot.resize_terminal(140, 46)
        await pilot.pause(0.4)
        assert not scr.has_class("compact-chat")
