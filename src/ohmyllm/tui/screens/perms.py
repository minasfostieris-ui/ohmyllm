"""Permission mode screen — the five modes, phrase-gated full access."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Label, OptionList, Static, Switch
from textual.widgets._option_list import Option

from ...config import PermissionMode

_MODE_ORDER = [
    PermissionMode.STRICT,
    PermissionMode.SAFE,
    PermissionMode.SANDBOXED,
    PermissionMode.DANGEROUS,
    PermissionMode.FULL_ACCESS,
]


class PermsScreen(Screen):
    BINDINGS = [Binding("escape", "close", "Back"), Binding("enter", "apply", "Apply", priority=True)]

    def compose(self) -> ComposeResult:
        yield Static("[b]🔐 Permission modes[/] — enforced by the platform; the model can never change them", id="screen-title")
        yield OptionList(id="mode-list")
        yield Static("", id="mode-desc", classes="detail-box")
        with Horizontal(classes="perm-row"):
            yield Label("Agent may write memories (soul.md): ")
            yield Switch(id="agent-memory-writes", value=True)
        with Horizontal(classes="perm-row"):
            yield Label("Require explicit user request before saving in strict mode: ")
            yield Switch(id="require-explicit", value=False)

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.clear_options()
        cur = self.app.svc.config.permission_mode
        for m in _MODE_ORDER:
            marker = "▶ " if m is cur else "  "
            style = {
                PermissionMode.STRICT: "bold cyan",
                PermissionMode.SAFE: "bold blue",
                PermissionMode.SANDBOXED: "bold white",
                PermissionMode.DANGEROUS: "bold yellow",
                PermissionMode.FULL_ACCESS: "bold white on red",
            }[m]
            ol.add_option(Option(f"[{style}]{marker}{m.label}[/]", id=m.value))
        ol.index = _MODE_ORDER.index(cur)
        self._show_desc()
        self.query_one("#agent-memory-writes", Switch).value = self.app.svc.config.memory.agent_writes
        self.query_one("#require-explicit", Switch).value = self.app.svc.config.memory.require_explicit_save

    def _show_desc(self) -> None:
        ol = self.query_one(OptionList)
        if ol.index is None:
            return
        m = _MODE_ORDER[ol.index]
        self.query_one("#mode-desc", Static).update(f"[b]{m.value}[/b] — {m.description}")

    def on_option_list_highlight(self) -> None:
        self._show_desc()

    def action_close(self) -> None:
        self.app.replace_screen("chat")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option and event.option.id:
            self._apply(PermissionMode(str(event.option.id)))

    def action_apply(self) -> None:
        ol = self.query_one(OptionList)
        if ol.index is not None:
            self._apply(_MODE_ORDER[ol.index])

    def _apply(self, mode: PermissionMode) -> None:
        app = self.app

        async def _go() -> None:
            if mode is PermissionMode.FULL_ACCESS:
                phrase = await app.prompt(
                    f"[red]FULL ACCESS[/red] — no routine approvals, tools run unattended.\nType the exact phrase: [b]{app.svc.full_access_phrase()}[/b]",
                    placeholder="confirmation phrase",
                )
            else:
                phrase = None
            ok = app.svc.set_mode(mode, phrase=phrase)
            if ok:
                app.notify(f"permission mode → {mode.value}", severity="warning" if mode.rank >= 3 else "information")
                app.refresh_chrome()
            else:
                app.notify("phrase mismatch — mode unchanged", severity="error")
                return
            self.action_close()

        self.run_worker(_go(), exclusive=False)

    @on(Switch.Changed, "#agent-memory-writes")
    def _toggle_writes(self, event: Switch.Changed) -> None:
        self.app.svc.config.memory.agent_writes = event.value
        self.app.svc.save_config()
        self.app.notify(f"agent memory writes {'on' if event.value else 'off'}")

    @on(Switch.Changed, "#require-explicit")
    def _toggle_explicit(self, event: Switch.Changed) -> None:
        self.app.svc.config.memory.require_explicit_save = event.value
        self.app.svc.save_config()
