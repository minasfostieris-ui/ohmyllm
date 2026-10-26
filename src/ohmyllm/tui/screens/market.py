"""Model marketplace screen: curated picks, VRAM fit, one-key download."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import OptionList, ProgressBar, Static
from textual.widgets.option_list import Option

from ...market import (
    CATALOG,
    DownloadState,
    MarketEntry,
    detect_vram_bytes,
    fit_verdict,
    vram_label,
)


class MarketScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("enter", "download", "Download", priority=True),
    ]

    CSS = """
    MarketScreen { padding: 1 2; }
    #market-list { height: 1fr; }
    #market-dl { height: auto; display: none; }
    .fit-fits { color: $success; }
    .fit-tight { color: $warning; }
    .fit-no { color: $error; }
    """

    def compose(self) -> ComposeResult:
        vram = detect_vram_bytes()
        yield Static(
            f"[b]🛒 Model marketplace[/] — curated picks for this CLI · enter/click download · esc back"
            f"  ·  [dim]{vram_label(vram)} detected[/dim]",
            id="screen-title",
        )
        yield OptionList(id="market-list")
        with Static(id="market-dl"):
            yield Static("", id="market-dl-label")
            yield ProgressBar(total=100, id="market-progress", show_bar=True)

    def on_mount(self) -> None:
        vram = detect_vram_bytes()
        lst = self.query_one(OptionList)
        for e in CATALOG:
            key, label = fit_verdict(e.size_gb, vram)
            style = {"fits": "[green]", "tight": "[yellow]", "no": "[red]", "unknown": "[dim]"}[key]
            lst.add_option(Option(
                f"{style}{e.title}[/] [b]{e.size_gb:.1f} GB[/] {style}({label})[/]\n"
                f"    [dim]{e.blurb[:150]}… · {e.repo}[/dim]",
                id=f"m-{e.mid}",
            ))

    # ------------------------------------------------------------- selection
    def _entry(self) -> MarketEntry | None:
        lst = self.query_one(OptionList)
        idx = lst.highlighted
        if idx is None:
            return None
        opt = lst.get_option_at_index(idx)
        mid = str(opt.id or "")
        for e in CATALOG:
            if f"m-{e.mid}" == mid:
                return e
        return None

    @on(OptionList.OptionSelected)
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._start_download()

    def action_download(self) -> None:
        self._start_download()

    def action_close(self) -> None:
        self._cancel = True
        self.app.pop_screen()

    # -------------------------------------------------------------- download
    def _start_download(self) -> None:
        e = self._entry()
        if e is None:
            return
        if getattr(self, "_worker_running", False):
            self.app.notify("a download is already running — esc cancels")
            return
        self._cancel = False
        self._worker_running = True
        box = self.query_one("#market-dl")
        box.display = True
        self.query_one("#market-progress", ProgressBar).update(total=100, progress=0)
        self.query_one("#market-dl-label", Static).update(
            f"downloading [b]{e.title}[/] ({e.size_gb:.1f} GB) → {__import__('pathlib').Path.home() / '.lmstudio/models'} …")
        self._run_download(e)

    @work(exclusive=True, group="market-dl")
    async def _run_download(self, e: MarketEntry) -> None:
        from ...market import download as dl

        state = DownloadState(entry=e)
        task = self.app.create_task(dl(e, state, cancel=lambda: getattr(self, "_cancel", False)))
        while not task.done():
            self._paint(state)
            await self._sleep_tick()
        self._paint(state)
        self._worker_running = False
        if state.error == "cancelled":
            self.query_one("#market-dl-label", Static).update("[red]download cancelled[/red]")
            self.app.notify("download cancelled")
        elif state.error:
            self.query_one("#market-dl-label", Static).update(f"[red]download failed:[/] {state.error}")
            self.app.notify(f"download failed: {state.error[:80]}", severity="error")
        else:
            self.query_one("#market-dl-label", Static).update(
                f"[green]✓ {e.title} ready[/green] — reload the model list ([b]/models[/]) and load it")
            self.app.notify(f"{e.file} downloaded — it will show up in Models")

    async def _sleep_tick(self) -> None:
        import asyncio

        await asyncio.sleep(0.25)

    def _paint(self, state: DownloadState) -> None:
        try:
            bar = self.query_one("#market-progress", ProgressBar)
        except Exception:  # noqa: BLE001 — screen may be closing
            return
        bar.update(progress=state.frac * 100)
        lbl = self.query_one("#market-dl-label", Static)
        got = state.received / 1e9
        tot = state.total / 1e9
        eta = f" · {state.eta_s:.0f}s left" if state.eta_s and not state.done else ""
        lbl.update(f"downloading [b]{state.entry.title}[/]  {got:.2f}/{tot:.1f} GB · "
                   f"[b]{state.mbps:.1f} MB/s[/]{eta}")
