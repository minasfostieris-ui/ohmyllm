"""Doctor screen: live diagnostics panel."""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets._option_list import Option

from ...doctor import run_checks, summarize


class DoctorScreen(Screen):
    BINDINGS = [Binding("escape", "close", "Back"), Binding("r", "rerun", "Re-run")]

    def compose(self) -> ComposeResult:
        yield Static("[b]🩺 Doctor[/] — r re-run · esc back", id="screen-title")
        yield OptionList(id="doctor-list")
        yield Static("", id="doctor-summary", classes="detail-box")

    def on_mount(self) -> None:
        self.query_one("#doctor-summary", Static).update("running checks…")
        self.check()

    def action_close(self) -> None:
        self.app.replace_screen("chat")

    def action_rerun(self) -> None:
        self.check()

    @work(exclusive=True)
    async def check(self) -> None:
        checks = await asyncio.to_thread(run_checks, self.app.svc.config_store, self.app.svc.config)
        try:
            ol = self.query_one("#doctor-list", OptionList)
        except Exception:  # noqa: BLE001 — screen closed mid-check
            return
        ol.clear_options()
        for c in checks:
            style = {"pass": "green", "warn": "yellow", "fail": "bold red", "info": "dim"}[c.status]
            text = f"[{style}]{c.icon}[/] [b]{c.label}[/b]  {c.detail}"
            if c.fix:
                text += f"\n      [dim]fix: {c.fix}[/dim]"
            ol.add_option(Option(text, id=c.id))
        self.query_one("#doctor-summary", Static).update(
            f"summary: {summarize(checks)}  ·  soul.md has {len(self.app.svc.soul.entries())} memories"
        )
