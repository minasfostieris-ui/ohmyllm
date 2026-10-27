"""Model manager screen: list, search, set default, per-model settings, test, pull."""

from __future__ import annotations

import time

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Input, ProgressBar, Static

from ...backends.base import BackendError, ModelInfo
from ...capabilities import family, quant_bits, thinking_support
from ...vision import cached_verdict as vision_cached
from ...vram import detect_gpus, human_bytes

_THINK_ORDER = {"levels": 0, "forced": 1, "on-off": 2, "none": 3, None: 4}


def sort_models(models: list[ModelInfo], key: str) -> list[ModelInfo]:
    if key == "size":
        return sorted(models, key=lambda m: m.size_bytes or 0, reverse=True)
    if key == "params":
        return sorted(models, key=lambda m: m.params_b or 0, reverse=True)
    if key == "quant":
        return sorted(models, key=lambda m: quant_bits(m.quant))
    if key == "family":
        return sorted(models, key=lambda m: (family(m.name), m.name.lower()))
    if key == "thinking":
        return sorted(models, key=lambda m: _THINK_ORDER.get(m.thinking or thinking_support(m.name, m.params_b), 4))
    if key == "vision":
        return sorted(models, key=lambda m: (m.vision is not True, m.vision is None, m.name.lower()))
    return sorted(models, key=lambda m: m.name.lower())


class ModelsScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("enter", "choose", "Set default"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "settings", "Model settings"),
        Binding("t", "test", "Test model"),
        Binding("l", "pull", "Pull (Ollama)"),
        Binding("o", "sort_cycle", "Sort"),
        Binding("v", "verify_vision", "Verify vision"),
        Binding("b", "backend", "Switch backend"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[b]🧠 Models[/] — enter choose · s settings · t test · l pull · r refresh · o sort · v verify vision · / filter · esc back", id="screen-title")
        yield Input(placeholder="filter models… (just start typing)", id="model-search")
        yield DataTable(id="models-table", cursor_type="row")
        yield ProgressBar(total=100, id="pull-progress", show_bar=True)
        yield Static("model server status & pull progress appear here", id="models-detail", classes="detail-box")

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        for label, w in (("model", 30), ("size·q", 13), ("par", 5), ("vram", 8),
                         ("fit", 5), ("tools", 5), ("vision", 6), ("think", 5)):
            t.add_column(label, width=w)
        t.focus()
        self._models: list[ModelInfo] = []
        self._visible: list[str] = []
        self._sort = "name"
        self.refresh_models()
        self._status("probing backend…")

    def _status(self, msg: str) -> None:
        try:
            self.query_one("#models-detail", Static).update(msg)
        except NoMatches:
            pass

    def _backend_names(self) -> list[str]:
        from ...providers import any_key

        cl = self.app.svc.config.cloud
        names = ["ollama", "lmstudio"]
        if any_key(cl):
            names.append("cloud")
        return names

    def _be_obj(self):
        return self.app.svc.backend(getattr(self, "_be", "") or None)

    def action_backend(self) -> None:
        names = self._backend_names()
        cur = getattr(self, "_be", "") or self.app.svc.config.default_backend.value
        try:
            nxt = names[(names.index(cur) + 1) % len(names)]
        except ValueError:
            nxt = names[0]
        self._be = nxt
        self.app.notify(f"listing [b]{nxt}[/] models")
        self.refresh_models()

    @work(exclusive=True)
    async def refresh_models(self) -> None:
        be = self._be_obj()
        try:
            self.query_one("#screen-title", Static).update(
                f"[b]🧠 Models[/] · [cyan]{be.display_name}[/] — enter choose · b backend · s settings · "
                "t test · l pull · r refresh · o sort · v verify vision · / filter · esc back")
        except NoMatches:
            pass
        ok, info = await be.health()
        if not ok:
            res = await be.ensure_running()
            ok = res.ok
            if not ok:
                self._models = []
                self._populate()
                self._status(f"[b]{be.display_name} is not running[/b]: {res.message}\n" + "\n".join(res.instructions))
                return
        try:
            self._models = sorted(await be.list_models(), key=lambda m: m.name)
        except BackendError as exc:
            self._models = []
            self._status(f"[red]{exc}[/red]")
        self._populate()
        gen = self.app.svc.config.generation
        weak = [m for m in self._models if not m.tool_capable and m.why_tool_weak()]
        extra = ""
        if weak:
            extra = "\n" + "\n".join(f"[yellow]⚠ {m.name}:[/] {m.why_tool_weak()}" for m in weak[:4])
        self._status(
            f"{be.display_name}: [b]{len(self._models)}[/b] model(s) · "
            f"ctx {gen.num_ctx} · {info.get('version', info.get('base_url', ''))}{extra}"
        )

    def _populate(self) -> None:
        try:
            t = self.query_one(DataTable)
        except NoMatches:
            return
        prev = self._selected_name()  # before clear(): clear() resets the cursor
        t.clear()
        self._visible = []
        self._models = sort_models(self._models, getattr(self, "_sort", "name"))
        q = self.query_one("#model-search", Input).value.strip().lower()
        cur = self.app.svc.current_model()
        gpus = detect_gpus()
        gpu_total = sum(g.total_bytes for g in gpus) or None
        frees = [g.free_bytes for g in gpus if g.free_bytes is not None]
        gpu_free = sum(frees) if frees else None
        gen = self.app.svc.config.generation
        for m in self._models:
            if q and q not in m.name.lower():
                continue
            est = m.vram_estimate(gen, gpu_total, gpu_free)
            fit = est.status if est else "?"
            style = {"comfortable": "green", "tight": "yellow", "too_large": "red"}.get(fit, "dim")
            key = getattr(self, "_be", "") or self.app.svc.backend_name_str()
            tested = vision_cached(key, m.name)
            if m.name in getattr(self, "_probing", set()):
                vis = "[yellow]⏳[/]"
            elif tested is not None:
                vis = "[green]👁[/]" if tested else "[dim]✗v[/]"
            elif m.vision is True:
                vis = "[green]👁[/]"
            elif m.vision is False:
                vis = "[dim]✗v[/]"
            else:
                vis = "[dim]?v[/]"
            th = m.thinking or thinking_support(m.name, m.params_b)
            th_cell = {"levels": "[green]⚡[/]", "forced": "[cyan]★[/]",
                       "on-off": "[yellow]◐[/]", "none": "[dim]✗[/]"}.get(th or "", "[dim]?[/]")
            fit_cell = {"comfortable": "✓", "tight": "⚠", "too_large": "✗"}.get(fit, "?")
            cap_cell = {True: "✓", False: "·", None: "?"}[m.tool_capable]
            sz = f"{human_bytes(m.size_bytes).replace(' ', '')}·{(m.quant or '?')[:4]}" if m.size_bytes else "?"
            vr = human_bytes(est.min_bytes).replace(" ", "") if est else "?"
            t.add_row(
                ("★" if m.name == cur else "") + m.name[:29],
                sz,
                f"{m.params_b:g}B" if m.params_b else "?",
                vr,
                f"[{style}]{fit_cell}[/]",
                cap_cell,
                vis,
                th_cell,
            )
            self._visible.append(m.name)
        if t.row_count:
            # keep the user's selection across refreshes — losing it made
            # actions fire on row 0 (the wrong-model settings bug)
            row = self._visible.index(prev) if prev in self._visible else 0
            t.move_cursor(row=row)

    def _selected_name(self) -> str | None:
        try:
            t = self.query_one(DataTable)
            if not t.row_count or not self._visible:
                return None
            return self._visible[min(t.cursor_coordinate.row, len(self._visible) - 1)]
        except NoMatches:
            return None

    def _selected(self) -> ModelInfo | None:
        t = self.query_one(DataTable)
        if not t.row_count or not self._visible:
            return None
        name = self._visible[min(t.cursor_coordinate.row, len(self._visible) - 1)]
        return next((m for m in self._models if m.name == name), None)

    # -------------------------------------------------------------- actions
    SORTS = ("name", "size", "params", "quant", "family", "thinking", "vision")

    def action_sort_cycle(self) -> None:
        cur = getattr(self, "_sort", "name")
        self._sort = self.SORTS[(self.SORTS.index(cur) + 1) % len(self.SORTS)]
        self._populate()
        self._status(f"sorted by [b]{self._sort}[/]")

    def action_verify_vision(self) -> None:
        m = self._selected()
        if m is None:
            return
        # re-probing is allowed: the disk sidecar can lie (LM Studio may not
        # wire the projector) — only a passed probe is a guarantee

        self._probing = getattr(self, "_probing", set()) | {m.name}
        self._populate()
        self._verify(m.name)

    @work(exclusive=True, group="vision")
    async def _verify(self, name: str) -> None:
        from ...vision import probe_vision, save_verdict

        be = self._be_obj()
        self._status(f"👁 probing [b]{name}[/] — loading it and reading two random numbers "
                     f"(up to a minute on cold models)…")
        try:
            verdict = await probe_vision(be, name)
        except Exception as exc:  # noqa: BLE001
            self._status(f"[red]probe failed:[/] {str(exc)[:120]}")
            self._probing = getattr(self, "_probing", set()) - {name}
            self._populate()
            return
        save_verdict(f"{be.name.value}:{name}", verdict)
        self._probing = getattr(self, "_probing", set()) - {name}
        self._status(f"👁 [b]{name}[/] {'[green]CAN see images[/green]' if verdict else '[red]cannot see images[/red]'} — verdict cached forever")
        self._populate()

    def action_close(self) -> None:
        self.app.replace_screen("chat")

    def action_refresh(self) -> None:
        self.refresh_models()

    def action_choose(self) -> None:
        m = self._selected()
        if m is None:
            return
        be = self._be_obj()
        key = be.name.value
        cfg = self.app.svc.config
        section = getattr(cfg, key, None)
        if section is not None and hasattr(section, "default_model"):
            section.default_model = m.name
            # choosing a model from the listed backend means "use it": switch
            # the default backend too, or a cloud pick would never reach chat
            cfg.default_backend = be.name
            self.app.svc.save_config()
            self._populate()
            if f"{key}:{m.name}" not in self.app.svc.config.model_settings:
                # first time this model is picked: ask for its per-model settings
                self.app.notify(f"{m.name} is new here — set its per-model settings")
                self._open_settings(m)
            else:
                self.app.notify(f"default model → {key}:{m.name}")

    def action_settings(self) -> None:
        m = self._selected()
        if m is None:
            return
        self._open_settings(m)

    def _open_settings(self, m: ModelInfo) -> None:
        from .modals import ModelSettingsModal

        def _done(saved: bool) -> None:
            if saved:
                self.app.notify(f"settings saved for {m.name}")
                self.refresh_models()

        self.app.push_screen(ModelSettingsModal(m), _done)

    def action_test(self) -> None:
        m = self._selected()
        if m is None:
            return
        self._test_model(m)

    @work(exclusive=True)
    async def _test_model(self, m: ModelInfo) -> None:
        be = self._be_obj()
        gen = self.app.svc.config.settings_for(be.name, m.name)
        t0 = time.perf_counter()
        self._status(f"testing {m.name}…")
        try:
            text, dur = await be.generate(
                m.name, [{"role": "user", "content": "Reply with exactly: OK"}], gen
            )
            ms = (time.perf_counter() - t0) * 1000
            self._status(f"[green]✔ test {m.name}[/green] in {ms:.0f}ms → {text[:120]!r}")
        except BackendError as exc:
            self._status(f"[red]✖ test {m.name}: {exc}[/red]")

    def action_pull(self) -> None:
        async def _go() -> None:
            be = self._be_obj()
            name = await self.app.prompt(f"Pull a model via {be.display_name}", "e.g. qwen2.5:7b-instruct")
            if name:
                self._pull(name)

        self.run_worker(_go(), exclusive=False)

    @work(exclusive=True)
    async def _pull(self, name: str) -> None:
        be = self._be_obj()
        bar = self.query_one("#pull-progress", ProgressBar)
        bar.display = True
        self._status(f"pulling {name}…")

        def prog(status: str, pct: float | None) -> None:
            if pct is not None:
                bar.update(total=100, progress=pct * 100)
            self._status(f"pulling {name}: {status}")

        try:
            await be.pull(name, prog)
            self._status(f"[green]✔ pulled {name}[/green]")
            self.app.notify(f"pulled {name}")
            self.refresh_models()
        except BackendError as exc:
            hint = (
                "\nLM Studio: open the app → Discover tab → search & install, or import a GGUF file."
                if be.name.value == "lmstudio"
                else ""
            )
            self._status(f"[red]{exc}[/red]{hint}")
        finally:
            bar.display = False

    @on(Input.Changed, "#model-search")
    def _search_changed(self) -> None:
        self._populate()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        # click or Enter on a row sets it as the default model
        self.action_choose()
