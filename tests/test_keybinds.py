"""Every advertised keybind must actually fire its action — all screens."""

from __future__ import annotations

import pytest
from textual.widgets import Input

from ohmyllm.tui.app import OhMyLLMApp

# (screen | None=app-level, key, action, call_original)
CASES: list[tuple[str | None, str, str, bool]] = [
    # models screen — the hint bar promises each of these
    ("models", "enter", "choose", False),
    ("models", "r", "refresh", True),
    ("models", "s", "settings", False),
    ("models", "t", "test", False),
    ("models", "l", "pull", False),
    ("models", "o", "sort_cycle", True),
    ("models", "v", "verify_vision", False),
    ("models", "b", "backend", True),
    ("models", "escape", "close", True),
    # backend
    ("backend", "r", "refresh", True),
    ("backend", "escape", "close", True),
    # providers
    ("providers", "enter", "connect", False),
    ("providers", "u", "use", False),
    ("providers", "x", "forget", False),
    ("providers", "c", "custom", False),
    ("providers", "e", "edit", False),
    ("providers", "r", "refresh", True),
    ("providers", "escape", "close", True),
    # market
    ("market", "enter", "download", False),
    ("market", "escape", "close", True),
    # help / memory / perms / doctor / settings
    ("help", "escape", "close", True),
    ("memory", "ctrl+enter", "save", True),
    ("memory", "escape", "close", True),
    ("perms", "enter", "apply", True),
    ("perms", "escape", "close", True),
    ("doctor", "r", "rerun", True),
    ("doctor", "escape", "close", True),
    ("settings", "escape", "close", True),
    # chats
    ("chats", "enter", "open", False),
    ("chats", "n", "new", False),
    ("chats", "r", "rename", False),
    ("chats", "p", "pin", True),
    ("chats", "x", "delete", False),
    ("chats", "e", "export_md", True),
    ("chats", "j", "export_json", True),
    ("chats", "t", "toggle_filter", True),
    ("chats", "escape", "close", True),
    # wizard
    ("wizard", "enter", "next", True),
    ("wizard", "escape", "skip", True),
    # chat screen
    (None, "up", "slash_prev", True),
    (None, "down", "slash_next", True),
    (None, "tab", "slash_complete", True),
    (None, "ctrl+t", "toggle_thinking", True),
    (None, "ctrl+v", "paste_image", True),
    (None, "ctrl+e", "talk", True),
    # app-level (priority) bindings
    (None, "ctrl+m", "screen_models", True),
    (None, "ctrl+b", "screen_backend", True),
    (None, "ctrl+p", "screen_perms", True),
    (None, "ctrl+s", "screen_memory", True),
    (None, "ctrl+d", "screen_doctor", True),
    (None, "ctrl+h", "screen_help", True),
    (None, "ctrl+o", "screen_chats", True),
    (None, "ctrl+n", "new_chat", True),
    (None, "ctrl+g", "screen_settings", True),
    (None, "ctrl+x", "cancel_turn", True),
    (None, "f2", "screen_chats", True),
]


def _action_name(action: str) -> str:
    return "action_" + action.replace("-", "_").split("(", 1)[0]


@pytest.mark.parametrize("screen,key,action,call_orig", CASES)
async def test_keybind_fires_action(oml_env, svc, screen, key, action, call_orig):
    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        if screen:
            app.replace_screen(screen)
            await pilot.pause(0.9)
        target = app.screen
        name = _action_name(action)
        orig = getattr(target, name, None) or getattr(app, name, None)
        assert orig is not None, f"{action!r} missing on {screen or 'chat/app'}"
        fired: list[bool] = []
        holder = target if hasattr(target, name) else app

        def wrapper(*a, **k):
            fired.append(True)
            return orig(*a, **k) if call_orig else None

        setattr(holder, name, wrapper)
        await pilot.press(key)
        await pilot.pause(0.5)
        assert fired, f"key {key!r} did not fire {action!r} on {screen or 'chat/app'}"


async def test_models_type_to_filter(oml_env, svc):
    # the hint says "just start typing" — letters must reach the filter
    # WITHOUT stealing command keys (o/b/r/s/t/l/v stay bound)
    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        app.replace_screen("models")
        await pilot.pause(1.2)
        scr = app.screen
        for c in "gemma":
            await pilot.press(c)
        await pilot.pause(0.8)
        assert scr.query_one("#model-search", Input).value == "gemma"
        assert all(m is None or "gemma" in m.name.lower() for m in scr._rows)
        await pilot.press("backspace")
        await pilot.pause(0.5)
        assert scr.query_one("#model-search", Input).value == "gemm"
        before = scr._sort
        await pilot.press("o")
        await pilot.pause(0.5)
        assert scr._sort != before, "sort key must still work while filtering"
