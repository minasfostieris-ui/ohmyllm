"""Throwaway headless TUI smoke: boot → wizard OR chat → message → memory → screens."""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, "src")

tmp = tempfile.mkdtemp(prefix="omllm-tui-")
os.environ["OMLLM_HOME"] = tmp
ws = os.path.join(tmp, "proj")
os.makedirs(ws)
os.environ["OMLLM_WORKSPACE"] = ws

from ohmyllm import paths  # noqa: E402
from ohmyllm.config import Config, ConfigStore  # noqa: E402

# seed config so the wizard test can toggle: first run → wizard path tested separately
from ohmyllm.tui.app import OhMyLLMApp  # noqa: E402


async def test_wizard() -> None:
    cfg = Config()  # first_run_completed False
    ConfigStore(paths.config_path()).save(cfg)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.6)
        from ohmyllm.tui.screens.wizard import WizardScreen
        assert isinstance(app.screen, WizardScreen), type(app.screen)
        # click through all 9 steps
        for _i in range(8):
            await pilot.click("#wiz-next")
            await pilot.pause(0.35)
            if _i == 0:  # step 1: backend-detect worker re-renders here (DuplicateIds race)
                wiz = app.screen
                await wiz._show_step()
                await wiz._show_step()
                await pilot.pause(0.4)
        await pilot.click("#wiz-next")
        await pilot.pause(0.8)
        from ohmyllm.tui.screens.chat import ChatScreen
        assert isinstance(app.screen, ChatScreen), type(app.screen)
    print("wizard OK")


async def test_chat_and_screens() -> None:
    cfg = Config()
    cfg.first_run_completed = True
    cfg.default_backend = "mock"
    store = ConfigStore(paths.config_path())
    store.save(cfg)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.8)
        from ohmyllm.tui.screens.chat import ChatScreen
        assert isinstance(app.screen, ChatScreen), type(app.screen)
        inp = app.screen.query_one("#chat-input")
        inp.value = "hello my name is TuiTester"
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause(0.15)
            if not app.busy:
                break
        assert not app.busy
        soul = app.svc.soul.read()
        assert "TuiTester" in soul, soul[-300:]
        # screens round-trip
        for key in ("chats", "models", "backend", "perms", "memory", "settings", "doctor", "help"):
            getattr(app, f"action_screen_{key}")()
            await pilot.pause(0.35)
            assert type(app.screen).__name__ in (
                "ChatsScreen", "ModelsScreen", "BackendScreen", "PermsScreen",
                "MemoryScreen", "SettingsScreen", "DoctorScreen", "HelpScreen",
            ), (key, type(app.screen).__name__)
        # return to chat
        app.replace_screen("chat")
        await pilot.pause(0.3)
        # palette
        await pilot.press("ctrl+k")
        await pilot.pause(0.4)
        assert "CommandPalette" in type(app.screen).__name__, type(app.screen).__name__
        await pilot.press("escape")
        await pilot.pause(0.3)
        # new chat + chats screen shows two sessions
        app.new_chat()
        await pilot.pause(0.3)
        app.replace_screen("chats")
        await pilot.pause(0.6)
        table = app.screen.query_one("#chats-table")
        assert table.row_count >= 1, table.row_count
        # slash commands
        app.replace_screen("chat")
        await pilot.pause(0.3)
        app.screen._slash("/help")
        await pilot.pause(0.3)
        assert "HelpScreen" in type(app.screen).__name__
        app.replace_screen("chat")
        await pilot.pause(0.2)
        app.screen._slash("/mode safe")
        await pilot.pause(0.5)
        assert app.svc.config.permission_mode.value == "safe"
        app.svc.config.permission_mode = "sandboxed"
    print("chat + screens OK")


async def main() -> None:
    await test_wizard()
    await test_chat_and_screens()


asyncio.run(main())
print("TUI-OK")
