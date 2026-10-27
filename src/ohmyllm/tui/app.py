"""OhMyLLM TUI application shell."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from textual import work
from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Input

from ..config import PermissionMode
from ..core import Services
from ..sessions import Session, make_title
from ..vram.detect import detect_gpus, human_bytes
from .widgets import ApprovalModal, ConfirmModal, ModeBanner, PromptModal


def _patch_input_paste() -> None:
    """Textual's built-in ctrl+V paste queries the clipboard over OSC-52,
    which most terminals ignore — every Input in the app silently swallowed
    the key. Route the action through the real clipboard tools instead."""
    from textual.widgets import Input

    if getattr(Input, "_omllm_paste", False):
        return

    def action_paste(self) -> None:
        import asyncio

        from .imagepaste import grab_clip_text

        async def worker() -> None:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, grab_clip_text)
            if not text:
                self.app.notify("clipboard has no text", severity="warning", timeout=4)
                return
            start, end = self.selection
            self.value = self.value[:start] + text + self.value[end:]
            self.cursor_position = start + len(text)

        if self.app is not None:
            self.app.call_later(worker)

    Input.action_paste = action_paste
    Input._omllm_paste = True


_patch_input_paste()


class OhMyLLMApp(App[None]):
    TITLE = "OhMyLLM"
    COMMAND_PALETTE_BINDING = "ctrl+k"
    ENABLE_COMMAND_PALETTE = True

    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("ctrl+q", "safe_quit", "Quit", priority=True),
        Binding("ctrl+c", "copy_selection", "Copy selection", priority=True),
        Binding("ctrl+m", "screen_models", "Models", priority=True),
        Binding("ctrl+b", "screen_backend", "Backend", priority=True),
        Binding("ctrl+p", "screen_perms", "Permissions", priority=True),
        Binding("ctrl+s", "screen_memory", "Memory", priority=True),
        Binding("ctrl+d", "screen_doctor", "Doctor", priority=True),
        Binding("ctrl+h", "screen_help", "Help", priority=True),
        Binding("ctrl+o", "screen_chats", "Chats", priority=True),
        Binding("ctrl+n", "new_chat", "New chat", priority=True),
        Binding("ctrl+g", "screen_settings", "Settings", priority=True),
        Binding("ctrl+x", "cancel_turn", "Cancel turn", priority=True),
        Binding("f2", "screen_chats", "Chats", priority=True),

]

    def __init__(self, workspace: str | None = None, resume_sid: str | None = None) -> None:
        super().__init__()
        self._workspace = workspace
        self._resume_sid = resume_sid
        self.svc: Services
        self.session: Session | None = None
        self.drafts: dict[str, str] = {}
        self.busy = False
        self.cancel_event = asyncio.Event()
        self.backend_ok = False
        self.tok_s = 0.0
        self.vram_text = ""
        self.save_state = True
        self.memory_flash_until = 0.0
        self.interjections: list[str] = []
        self.turn_active = False
        self.turn_text = ""
        self.turn_thinking = ""

    # ------------------------------------------------------------------ boot
    def on_mount(self) -> None:
        self.svc = Services.boot(self._workspace)
        self._sync_vram_text()
        self._health_probe()
        if not self.svc.config.first_run_completed:
            from .screens.wizard import WizardScreen

            self.push_screen(WizardScreen())
        else:
            self.open_resume_or_new()

    def open_resume_or_new(self, sid: str | None = None) -> None:
        from .screens.chat import ChatScreen

        sid = sid or self._resume_sid
        sess = self.svc.sessions.get(sid) if sid else None
        if sess is None:
            sess = self.svc.new_session()
        self.session = sess
        self._resume_sid = None
        if len(self.screen_stack) <= 1:
            self.push_screen(ChatScreen())
        else:
            self.switch_screen(ChatScreen())

    def load_session(self, sid: str) -> bool:
        sess = self.svc.sessions.get(sid)
        if sess is None:
            return False
        self._stash_draft()
        self.session = sess
        from .screens.chat import ChatScreen

        self.switch_screen(ChatScreen())
        return True

    def new_chat(self, first_message: str = "") -> None:
        self._stash_draft()
        self.session = self.svc.new_session(first_message)
        self.save_state = False
        from .screens.chat import ChatScreen

        self.switch_screen(ChatScreen())

    def replace_screen(self, name: str) -> None:
        self._stash_draft()
        from .screens import SCREENS

        self.switch_screen(SCREENS[name]())

    def _stash_draft(self) -> None:
        if self.session is None:
            return
        try:
            chats = list(self.screen_stack)
        except Exception:  # noqa: BLE001
            return
        for s in chats:
            try:
                inp = s.query_one(Input)
            except Exception:  # noqa: BLE001
                continue
            if inp and getattr(inp, "placeholder", "") and "ask" in (inp.placeholder or ""):
                self.drafts[self.session.id] = inp.value
                return

    # -------------------------------------------------------------- chrome
    def status_state(self) -> dict[str, Any]:
        if not hasattr(self, "svc"):
            return {}
        sess = self.session
        ctx_used = 0
        if sess:
            ctx_used = sum(len(str(m.get("content", ""))) for m in sess.messages) // 4
        snap = self.svc.status_snapshot(tok_s=self.tok_s, ctx_used=ctx_used)
        snap.update(
            backend_ok=self.backend_ok,
            busy=self.busy,
            vram=self.vram_text,
            saved=self.save_state,
            memory=self.svc.config.memory.enabled and self.svc.config.memory.agent_writes,
            session=sess.id if sess else "…",
        )
        return snap

    def dispatch_turn_event(self, ev: dict[str, Any]) -> None:
        """Single funnel for runtime events: keeps the in-flight buffers on the
        app so switching screens mid-turn can never eat the response."""
        t = ev.get("type")
        if t == "turn_start":
            self.turn_active = True
            self.turn_text = ""
            self.turn_thinking = ""
        elif t == "token":
            self.turn_text += str(ev.get("text", ""))
        elif t == "thinking_text":
            self.turn_thinking += str(ev.get("text", ""))
        elif t == "ultrathink_review":
            self.turn_text = ""
            self.turn_thinking = ""
        elif t == "turn_end":
            self.turn_active = False
        from .screens.chat import ChatScreen
        scr = self.screen
        if isinstance(scr, ChatScreen):
            scr._on_event(ev)

    def reload_model(self, backend_name: str, model: str) -> None:
        """num_gpu/num_ctx are LOAD-time settings: apply them by re-ejecting
        and re-loading the model now, instead of silently ignoring the change."""
        async def _r() -> None:
            try:
                be = self.svc.backend(backend_name)
                if backend_name == "lmstudio":
                    await be.unload(model)
                    self._notice(f"reloading {model} with the new GPU/context settings…")
                    await be.ensure_loaded(model)
                    self._notice(f"{model} reloaded — new settings active")
                elif backend_name == "ollama":
                    import httpx
                    url = self.svc.config.ollama.host.rstrip("/")
                    async with httpx.AsyncClient(timeout=30.0) as c:
                        await c.post(url + "/api/generate", json={"model": model, "keep_alive": 0})
                    self._notice(f"{model} unloaded — new settings apply on your next message")
            except Exception as exc:  # noqa: BLE001
                self._notice(f"model reload failed: {exc}")
        self.run_worker(_r(), exclusive=False)

    def refresh_chrome(self) -> None:
        mode = self.svc.config.permission_mode.value
        for s in self.screen_stack:
            for b in s.query(ModeBanner):
                b.set_mode(mode)

    def _sync_vram_text(self) -> None:
        try:
            gpus = detect_gpus()
            if not gpus:
                self.vram_text = "GPU: none"
                return
            total = sum(g.total_bytes for g in gpus)
            free = [g.free_bytes for g in gpus if g.free_bytes is not None]
            self.vram_text = f"GPU {human_bytes(total)}" + (f" / {human_bytes(sum(free))} free" if free else "")
        except Exception:  # noqa: BLE001
            self.vram_text = ""

    def _notice(self, text: str) -> None:
        """Route a startup/probe notice into the chat as a plain sysline.

        Toast overlays draw on top of the chat/panel separator and look like
        a notch cut into it; the status bar already carries backend state.
        """
        scr = self.screen
        fn = getattr(scr, "_sysline", None)
        if callable(fn):
            fn(text, "error")
        else:
            self.notify(text, severity="warning", timeout=6)

    @work(exclusive=True)
    async def _health_probe(self) -> None:
        try:
            be = self.svc.backend()
            ok, _info = await be.health()
            if not ok and self.svc.config.default_backend.value != "mock":
                res = await be.ensure_running()
                ok = res.ok
                if not res.ok:
                    self._notice(f"{be.display_name}: {res.message}")
            self.backend_ok = ok
        except Exception as exc:  # noqa: BLE001
            self.backend_ok = False
            self._notice(f"backend probe failed: {exc}")

    # ----------------------------------------------------------- approvals
    async def request_approval(self, req: Any) -> Any:
        from ..safety import ApprovalOutcome

        if self.svc.config.permission_mode is PermissionMode.FULL_ACCESS:
            return ApprovalOutcome.APPROVED
        outcome = await self.push_screen_wait(ApprovalModal(req))
        return outcome if isinstance(outcome, ApprovalOutcome) else ApprovalOutcome.DENIED

    async def _modal(self, screen: Screen) -> Any:
        """push_screen_wait needs a worker; bindings are not workers. Callback +
        future works from actions, workers and call_next alike."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _done(result: Any) -> None:
            if not fut.done():
                fut.set_result(result)

        self.push_screen(screen, _done)
        return await fut

    async def prompt(self, title: str, placeholder: str = "", initial: str = "") -> str | None:
        return await self._modal(PromptModal(title, placeholder, initial))

    async def confirm(self, question: str, extra: str = "", danger: bool = False, ok_label: str = "Yes") -> bool:
        return bool(await self._modal(ConfirmModal(question, extra, ok_label=ok_label, danger=danger)))

    # -------------------------------------------------------------- actions
    async def _quit_and_release(self) -> None:
        import asyncio

        try:
            await asyncio.wait_for(self.svc.release_model(), timeout=15)
        except Exception:  # noqa: BLE001 — quit must not hang on a dead server
            pass
        self.exit()

    def action_copy_selection(self) -> None:
        """Ctrl+C copies the selection like every other terminal app — it does
        NOT quit (that is ctrl+Q). Quitting must never ambush a copy."""
        text = None
        with contextlib.suppress(Exception):
            text = self.screen.get_selected_text()
        if not text:
            self.notify("nothing selected — ctrl+Q quits, ctrl+V pastes images", timeout=3)
            return
        import subprocess

        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                    ["pbcopy"]):
            try:
                r = subprocess.run(cmd, input=text.encode(), timeout=3,
                                   capture_output=True)
                if r.returncode == 0:
                    self.notify(f"copied {len(text)} chars", timeout=2)
                    return
            except (OSError, subprocess.SubprocessError):
                continue
        self.notify("no clipboard tool found (wl-copy/xclip/pbcopy)", severity="warning")

    def action_safe_quit(self) -> None:
        # NOTE: must not await the modal here — binding handlers run inside the
        # message pump, and awaiting a modal would freeze every input (the old
        # ctrl+Q "stuck on Quit?" bug). run_worker keeps the pump free.
        if not hasattr(self, "svc"):
            self.exit()
            return
        if getattr(self, "_quit_prompt_open", False):
            return  # Ctrl+Q again must not stack a second modal
        if self.busy:
            self.notify("agent is working — Ctrl+X cancels the turn first", severity="warning")
            return
        if not self.svc.config.ui.confirm_quit:
            self.run_worker(self._quit_and_release(), exclusive=False)
            return

        async def _go() -> None:
            self._quit_prompt_open = True
            try:
                if await self.confirm("Quit OhMyLLM? Chats and memory are already saved.",
                                      ok_label="Quit", danger=True):
                    await self._quit_and_release()
            finally:
                self._quit_prompt_open = False

        self.run_worker(_go(), exclusive=False)

    def action_cancel_turn(self) -> None:
        self.cancel_event.set()
        self.notify("cancelling current turn…", severity="warning")

    def action_screen_models(self) -> None:
        self.replace_screen("models")

    def action_screen_backend(self) -> None:
        self.replace_screen("backend")

    def action_screen_perms(self) -> None:
        self.replace_screen("perms")

    def action_screen_memory(self) -> None:
        self.replace_screen("memory")

    def action_screen_doctor(self) -> None:
        self.replace_screen("doctor")

    def action_screen_help(self) -> None:
        self.replace_screen("help")

    def action_screen_chats(self) -> None:
        self.replace_screen("chats")

    def action_screen_settings(self) -> None:
        self.replace_screen("settings")

    # ------------------------------------------------------------ commands
    def get_system_commands(self, screen: Screen) -> list[SystemCommand]:
        return [
            SystemCommand("Open chat", "Return to the active conversation", lambda: self.replace_screen("chat")),
            SystemCommand("Browse chats", "Chats screen (Ctrl+O / F2)", lambda: self.replace_screen("chats")),
            SystemCommand("New chat", "Start a fresh session (Ctrl+N)", lambda: self.new_chat()),
            SystemCommand("Select model", "Model manager (Ctrl+M)", lambda: self.replace_screen("models")),
            SystemCommand("Backends and servers", "Ollama / LM Studio status (Ctrl+B)", lambda: self.replace_screen("backend")),
            SystemCommand("Providers", "Cloud API keys — OpenRouter, Anthropic, /providers", lambda: self.replace_screen("providers")),
            SystemCommand("Permission mode", "strict/safe/sandboxed/dangerous/full (Ctrl+P)", lambda: self.replace_screen("perms")),
            SystemCommand("Memory soul.md", "View and edit persistent memory (Ctrl+S)", lambda: self.replace_screen("memory")),
            SystemCommand("Settings", "Generation, safety, server config (Ctrl+G)", lambda: self.replace_screen("settings")),
            SystemCommand("Doctor", "Environment diagnostics (Ctrl+D)", lambda: self.replace_screen("doctor")),
            SystemCommand("Help", "Shortcuts and slash commands (Ctrl+H)", lambda: self.replace_screen("help")),
            SystemCommand("Export chat markdown", "Save current chat as .md", lambda: self._export("markdown")),
            SystemCommand("Export chat json", "Save current chat as .json", lambda: self._export("json")),
            SystemCommand("Copy last reply", "Clipboard: last assistant message", lambda: self._copy_last()),
            SystemCommand("Quit", "Safe quit", lambda: self.action_safe_quit()),
        ]

    def _export(self, fmt: str) -> None:
        from ..sessions.export import export

        if self.session is None:
            return
        path = export(self.svc.sessions, self.session, fmt)
        self.notify(f"exported → {path}", severity="information")

    def _copy_last(self) -> None:
        if self.session is None:
            return
        for m in reversed(self.session.messages):
            if m.get("role") == "assistant" and m.get("content"):
                self.copy_to_clipboard(str(m["content"]))
                self.notify("copied last reply")
                return
        self.notify("nothing to copy yet", severity="warning")

    def note_title(self, text: str) -> None:
        if self.session and self.session.title == "New chat":
            self.session.title = make_title(text)


__all__ = ["OhMyLLMApp"]
