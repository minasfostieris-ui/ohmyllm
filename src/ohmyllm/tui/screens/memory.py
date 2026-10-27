"""Memory / soul.md screen: view, edit, revert, clear, recent activity."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static, Switch, TextArea


class MemoryScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("ctrl+enter", "save", "Save"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[b]🧠 Memory — soul.md[/] — ctrl+enter save · esc back (agent writes via soul_* tools)", id="screen-title")
        with Vertical(classes="band", id="soul-band"):
            yield TextArea("", id="soul-area", language="markdown")
        with Horizontal(id="memory-buttons"):
            yield Button("save (backs up first)", id="mem-save", variant="primary")
            yield Button("revert newest backup", id="mem-revert")
            yield Button("clear memory", id="mem-clear", variant="error")
            yield Label(" agent writes:")
            yield Switch(id="mem-writes", value=True)
        yield Static("", id="recent-memory")

    def on_mount(self) -> None:
        soul = self.app.svc.soul
        soul.ensure()
        ta = self.query_one(TextArea)
        ta.load_text(soul.read())
        self.query_one("#mem-writes", Switch).value = self.app.svc.config.memory.agent_writes
        self._recent()

    def _recent(self) -> None:
        rows = self.app.svc.soul.recent(6)
        if not rows:
            self.query_one("#recent-memory", Static).update("[dim]no memory changes recorded yet[/dim]")
            return
        lines = [f"  {r.get('ts','')[:16]}  {r.get('action'):<8} {r.get('section','')} → {r.get('text','')[:60]}" for r in rows]
        self.query_one("#recent-memory", Static).update("[b]recent memory activity[/b]\n" + "\n".join(lines))

    def action_close(self) -> None:
        self.app.replace_screen("chat")

    def action_save(self) -> None:
        self._save()

    def _save(self) -> None:
        soul = self.app.svc.soul
        text = self.query_one(TextArea).text
        if text == soul.read():
            self.app.notify("no changes")
            return
        soul.backup()
        soul.path.write_text(text, encoding="utf-8")
        self.app.notify("soul.md saved (backup created)")
        self._recent()

    @on(Button.Pressed, "#mem-save")
    def _save_btn(self) -> None:
        self._save()

    @on(Button.Pressed, "#mem-revert")
    def _revert_btn(self) -> None:
        async def _go() -> None:
            soul = self.app.svc.soul
            backups = soul.backups()
            if not backups:
                self.app.notify("no backups available", severity="warning")
                return
            ok = await self.app.confirm(
                f"Revert to newest backup?\n{backups[0].name}",
                "The current soul.md is backed up first, so this is itself revertible.",
            )
            if ok:
                soul.revert()
                self.query_one(TextArea).load_text(soul.read())
                self.app.notify("reverted")
                self._recent()

        self.run_worker(_go(), exclusive=False)

    @on(Button.Pressed, "#mem-clear")
    def _clear_btn(self) -> None:
        async def _go() -> None:
            ok = await self.app.confirm(
                "[b]Clear all memories?[/b]",
                "soul.md resets to the empty template. A backup is written first (revert stays available).",
                danger=True,
                ok_label="Back up + clear",
            )
            if ok:
                self.app.svc.soul.clear()
                self.query_one(TextArea).load_text(self.app.svc.soul.read())
                self.app.notify("memory cleared (backup saved)")
                self._recent()

        self.run_worker(_go(), exclusive=False)

    @on(Switch.Changed, "#mem-writes")
    def _writes_changed(self, event: Switch.Changed) -> None:
        self.app.svc.config.memory.agent_writes = event.value
        self.app.svc.save_config()
        self.app.notify(f"agent memory writes {'enabled' if event.value else 'disabled'}")
