"""Model marketplace screen: curated picks, VRAM fit, one-key download."""

from __future__ import annotations

import contextlib

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import OptionList, ProgressBar, Static
from textual.widgets.option_list import Option

from ...market import (
    CATALOG,
    DownloadState,
    MarketEntry,
    detect_vram_bytes,
    fit_verdict,
    resolve,
    vram_gib,
)

_FIT_MARK = {"fits": "[green]✓[/green]", "tight": "[yellow]⚠[/yellow]",
             "no": "[red]✗[/red]", "unknown": "[dim]?[/dim]"}
_FIT_COLOR = {"fits": "green", "tight": "yellow", "no": "red", "unknown": "dim"}


def _gpu_headline(vram: int | None) -> str:
    """'AMD Radeon RX 6700 XT · 12 GB' when we know the name, else just size."""
    name = ""
    try:
        from ...vram import detect_gpus

        gpus = detect_gpus()
        if gpus:
            g = max(gpus, key=lambda g: g.total_bytes)
            if g.model and g.model not in ("unknown",) and not g.model.endswith("GPU 0"):
                name = g.model
    except Exception:  # noqa: BLE001 — cosmetic only
        pass
    g = vram_gib(vram)
    size = f"{g} GB" if g else "VRAM unknown"
    return f"[b]{name} · {size}[/b]" if name else f"[b]{size}[/b]"


class MarketScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("enter", "download", "Download", priority=True),
    ]

    CSS = """
    MarketScreen { padding: 1 2; }
    #market-list { height: auto; max-height: 60%; }
    #market-detail { height: 1fr; margin-top: 1; }
    #market-dl { height: auto; display: none; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        self._vram = detect_vram_bytes()
        yield Static(
            f"🛒 [b]Model marketplace[/b] — enter downloads · esc backs   ·   detected {_gpu_headline(self._vram)}",
            id="screen-title",
        )
        yield OptionList(id="market-list")
        yield Static("[dim]select a model to see the full pitch[/dim]", id="market-detail", classes="detail-box")
        with Static(id="market-dl"):
            yield Static("", id="market-dl-label")
            yield ProgressBar(total=100, id="market-progress", show_bar=True)

    def on_mount(self) -> None:
        lst = self.query_one(OptionList)
        for raw in CATALOG:
            e = resolve(raw)
            key, label = fit_verdict(e.size_gb, self._vram)
            col = _FIT_COLOR[key]
            lst.add_option(Option(
                f"[{col}]■[/] [b]{e.title}[/b]  [b]{e.size_gb:.1f} GB[/]  [{col}]{label}[/{col}]",
                id=f"m-{raw.mid}",
            ))
        lst.highlighted = 0
        self._paint_detail()

    # ------------------------------------------------------------- selection
    @on(OptionList.OptionHighlighted)
    def _hovered(self, event: OptionList.OptionHighlighted) -> None:
        self._paint_detail()

    def _entry_by_id(self, mid: str) -> MarketEntry | None:
        for e in CATALOG:
            if f"m-{e.mid}" == mid:
                return e
        return None

    def _selected_id(self) -> str:
        lst = self.query_one(OptionList)
        idx = lst.highlighted
        if idx is None:
            return ""
        return str(lst.get_option_at_index(idx).id or "")

    def _paint_detail(self) -> None:
        e = self._entry_by_id(self._selected_id())
        if e is None:
            return
        key, label = fit_verdict(e.size_gb, self._vram)
        badge = f"{_FIT_MARK[key]} {label}"
        extra = ""
        if key == "no" and e.kind == "gguf":
            extra = "\n[dim]tip: the repo ships smaller quants — see the HF page[/dim]"
        kind = "" if e.kind == "gguf" else f" · [yellow]{e.kind} format[/yellow]"
        if e.mtp:
            kind += " · [green]MTP drafter auto-downloads beside it[/green]"
        if e.alt is not None:
            import shutil

            have = bool(shutil.which("ninfer"))
            kind += (" · [green]NInfer build selected (runtime detected)[/green]" if have else
                     " · GGUF for LM Studio — NInfer MTP build auto-used if installed")
        try:
            self.query_one("#market-detail", Static).update(
                f"{badge}  ·  {e.repo}{kind}\n\n{e.blurb}{extra}\n\n"
                f"[dim]enter or click → downloads into ~/.lmstudio/models "
                f"(vision companion included)[/dim]")
        except NoMatches:
            pass

    def _entry(self) -> MarketEntry | None:
        return self._entry_by_id(self._selected_id())

    @on(OptionList.OptionSelected)
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._start_download()

    def action_download(self) -> None:
        self._start_download()

    def action_close(self) -> None:
        self._cancel = True
        # the app uses replace_screen (not push) to get here — pop would
        # empty the stack and brick the UI
        self.app.replace_screen("chat")

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
        self.query_one("#market-dl").display = True
        self.query_one("#market-progress", ProgressBar).update(total=100, progress=0)
        self.query_one("#market-dl-label", Static).update(f"downloading [b]{e.title}[/] …")
        self._run_download(e)

    @work(exclusive=True, group="market-dl")
    async def _run_download(self, e: MarketEntry) -> None:
        import asyncio

        from ...market import download as dl

        state = DownloadState(entry=e)
        task = asyncio.ensure_future(dl(e, state, cancel=lambda: getattr(self, "_cancel", False)))
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
        self.set_timer(4.0, self._hide_dl_box)

    def _hide_dl_box(self) -> None:
        with contextlib.suppress(Exception):
            box = self.query_one("#market-dl")
            if not getattr(self, "_worker_running", False):
                box.display = False

    async def _sleep_tick(self) -> None:
        import asyncio

        await asyncio.sleep(0.25)

    def _paint(self, state: DownloadState) -> None:
        try:
            bar = self.query_one("#market-progress", ProgressBar)
        except NoMatches:  # screen may be closing
            return
        bar.update(progress=state.frac * 100)
        lbl = self.query_one("#market-dl-label", Static)
        got = state.received / 1e9
        tot = state.total / 1e9
        eta = f" · {state.eta_s:.0f}s left" if state.eta_s and not state.done else ""
        lbl.update(f"downloading [b]{state.entry.title}[/]  {got:.2f}/{tot:.1f} GB · "
                   f"[b]{state.mbps:.1f} MB/s[/]{eta}")
