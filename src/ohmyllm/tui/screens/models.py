"""Model manager screen: every backend's models in one sectioned table.

Default view lists ALL reachable backends (ollama / lmstudio / cloud) as
labelled blocks; `b` narrows to a single backend. Rows carry their own
backend, so choose/test/verify/settings always act on the right server.
"""

from __future__ import annotations

import asyncio
import time

from rich.cells import set_cell_size
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.events import Key, Resize
from textual.screen import Screen
from textual.widgets import DataTable, Input, ProgressBar, Static

from ...backends.base import BackendError, BackendName, ModelInfo
from ...capabilities import family, quant_bits, thinking_support
from ...vision import cached_verdict as vision_cached
from ...vram import detect_gpus, detect_gpus_cached, human_bytes

_THINK_ORDER = {"levels": 0, "forced": 1, "on-off": 2, "none": 3, None: 4}
_SECTION_ORDER = [BackendName.OLLAMA, BackendName.LMSTUDIO, BackendName.CLOUD]


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
        Binding("enter", "choose", "Set default", priority=True),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "settings", "Model settings"),
        Binding("t", "test", "Test model"),
        Binding("l", "pull", "Pull (Ollama)"),
        Binding("o", "sort_cycle", "Sort"),
        Binding("v", "verify_vision", "Verify vision"),
        Binding("b", "backend", "Filter backend"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[b]🧠 Models[/] — enter choose · s settings · t test · l pull · r refresh · o sort · v verify vision · / filter · esc back", id="screen-title")
        yield Input(placeholder="filter models… (just start typing)", id="model-search")
        with Vertical(classes="band", id="models-band"):
            yield DataTable(id="models-table", cursor_type="row")
        yield ProgressBar(total=100, id="pull-progress", show_bar=True)
        yield Static("model server status & pull progress appear here", id="models-detail", classes="detail-box")

    def _fit_columns(self) -> None:
        """Name column = available width minus the fixed columns; the table
        can never overflow horizontally, so nothing is ever scrolled off."""
        try:
            t = self.query_one(DataTable)
        except NoMatches:
            return
        # measured: Textual pads every column 2 cells (8 cols = 16) plus the
        # vertical scrollbar (1), the band border (2) and the table's right
        # margin (1) — anything more and the last column clips
        avail = (t.size.width or 84) - 20
        fixed = 13 + 5 + 8 + 5 + 5 + 6 + 5
        try:
            t.columns[t.ordered_columns[0].key].width = max(16, avail - fixed)
        except (KeyError, IndexError):
            pass

    def on_resize(self, event: Resize) -> None:  # noqa: ARG002
        self._fit_columns()

    # keys that belong to commands, not to the filter text
    _PASS_KEYS = {"enter", "escape", "r", "s", "t", "l", "o", "v", "b"}

    def on_key(self, event: Key) -> None:
        """'just start typing' made real: the table holds focus, so printable
        keys are routed into the filter input here; command keys pass
        through to their bindings untouched."""
        ch = event.character
        if ch and ch.isprintable() and event.key not in self._PASS_KEYS:
            inp = self.query_one("#model-search", Input)
            inp.value += ch
            event.stop()
            return
        if event.key == "backspace":
            inp = self.query_one("#model-search", Input)
            if inp.value:
                inp.value = inp.value[:-1]
                event.stop()
        # everything else (enter, esc, r, s, t, l, o, v, b, cursors…) falls
        # through untouched to the default handler, which runs the bindings

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        for label, w in (("model", 30), ("size·q", 13), ("par", 5), ("vram", 8),
                         ("fit", 5), ("tools", 5), ("vision", 6), ("think", 5)):
            t.add_column(label, width=w)
        t.focus()
        self._models: list[ModelInfo] = []
        self._gpus: list = []
        self._rows: list[ModelInfo | None] = []  # None = section separator row
        self._visible: list[str] = []  # kept for cursor-restore compat
        self._sort = "name"
        self._filter = "all"
        self.query_one("#pull-progress", ProgressBar).display = False
        self.refresh_models()
        self._status("probing backends…")

    def _status(self, msg: str) -> None:
        try:
            self.query_one("#models-detail", Static).update(msg)
        except NoMatches:
            pass

    # ------------------------------------------------------------- backends
    def _sources(self) -> list[str]:
        """Backends to list for the current filter."""
        from ...providers import any_key

        cl = self.app.svc.config.cloud
        names = ["ollama", "lmstudio"]
        if any_key(cl):
            names.append("cloud")
        if self.app.svc.config.default_backend.value == "mock":
            names.append("mock")  # dev fixture: never hide the active backend
        if self._filter != "all":
            return [self._filter] if self._filter in names else names[:1]
        # the active backend's rows first: offline probes cost up to 3 s
        # each and must not delay the server the user actually uses
        cur = self.app.svc.config.default_backend.value
        names.sort(key=lambda n: n != cur)
        return names

    def _be_obj(self, name: str | None = None):
        if name is None:
            name = self._filter if self._filter != "all" else self.app.svc.config.default_backend.value
        return self.app.svc.backend(name)

    def action_backend(self) -> None:
        opts = ["all"] + self._backend_names_available()
        try:
            nxt = opts[(opts.index(self._filter) + 1) % len(opts)]
        except ValueError:
            nxt = "all"
        self._filter = nxt
        self.app.notify(f"listing [b]{nxt}[/] models")
        self.refresh_models()

    def _backend_names_available(self) -> list[str]:
        from ...providers import any_key

        names = ["ollama", "lmstudio"]
        if any_key(self.app.svc.config.cloud):
            names.append("cloud")
        return names

    # --------------------------------------------------------------- refresh
    @work(exclusive=True)
    async def refresh_models(self) -> None:
        self.app.svc.reload_config()  # pick up keys the GUI bridge just saved
        label = self._filter
        try:
            self.query_one("#screen-title", Static).update(
                f"[b]🧠 Models[/] · [cyan]{label}[/] — enter choose · b filter · s settings · "
                "t test · l pull · r refresh · o sort · v verify vision · / filter · esc back")
        except NoMatches:
            pass
        models: list[ModelInfo] = []
        notes: list[str] = []
        self._gpus = await asyncio.to_thread(detect_gpus)
        for name in self._sources():
            be = self.app.svc.backend(name)
            try:
                ok, info = await be.health()
                if not ok and self._filter == name:
                    # explicit single-backend view: try to wake the server up
                    res = await be.ensure_running()
                    ok = res.ok
                    if not ok:
                        notes.append(f"[b]{be.display_name} is not running[/b]: {res.message}")
                        continue
                if not ok:
                    notes.append(f"[dim]{be.display_name}: offline[/dim]")
                    continue
                listed = sorted(await be.list_models(), key=lambda m: m.name)
                for m in listed:
                    m.backend = be.name
                models.extend(listed)
            except BackendError as exc:
                notes.append(f"[red]{be.display_name}:[/] {str(exc)[:120]}")
            # progressive: rows land as each backend answers, not after the
            # slowest offline probe
            self._models = list(models)
            self._populate()
        self._models = models
        self._populate()
        counts = " · ".join(
            f"[b]{be}[/b] {sum(1 for m in models if m.backend.value == be)}"
            for be in dict.fromkeys(m.backend.value for m in models)) or "no models"
        gen = self.app.svc.config.generation
        cur = self.app.svc.current_model()
        curm = next((m for m in models if m.name == cur), None)
        ctx_note = (f"ctx {curm.ctx_len:,} (provider)" if curm and curm.ctx_len
                    else f"ctx {gen.num_ctx}")
        weak = [m for m in self._models if not m.tool_capable and m.why_tool_weak()]
        extra = ""
        if weak:
            extra = "\n" + "\n".join(f"[yellow]⚠ {m.name}:[/] {m.why_tool_weak()}" for m in weak[:4])
        self._status(f"{counts} · {ctx_note}" + ("\n" + "\n".join(notes) if notes else "") + extra)

    # -------------------------------------------------------------- populate
    def _populate(self) -> None:
        """Render rows. self._gpus is the probe result from the worker —
        detect_gpus_cached() alone would freeze fit/vram at '?' once the
        10 s TTL expired, and the probe must never run on the UI thread."""
        try:
            t = self.query_one(DataTable)
        except NoMatches:
            return
        self._fit_columns()
        try:
            name_w = t.ordered_columns[0].width
        except IndexError:
            name_w = 30
        prev = self._selected_name()  # before clear(): clear() resets the cursor
        t.clear()
        self._rows = []
        q = self.query_one("#model-search", Input).value.strip().lower()
        cfg = self.app.svc.config
        cur, cur_be = self.app.svc.current_model(), cfg.default_backend.value
        gpus = self._gpus or detect_gpus_cached()
        gpu_total = sum(g.total_bytes for g in gpus) or None
        frees = [g.free_bytes for g in gpus if g.free_bytes is not None]
        gpu_free = sum(frees) if frees else None
        order = {be: i for i, be in enumerate(_SECTION_ORDER)}
        # stable sort: keeps whatever within-section order _models already has
        grouped = sorted(self._models, key=lambda m: order.get(m.backend.value, 9))
        shown_be: BackendName | None = None
        for m in grouped:
            if q and q not in m.name.lower():
                continue
            if m.backend != shown_be:
                shown_be = m.backend
                t.add_row(f"[b dim]── {m.backend.value} ──[/]", "", "", "", "", "", "", "")
                self._rows.append(None)
            est = m.vram_estimate(cfg.settings_for(m.backend, m.name), gpu_total, gpu_free)
            fit = est.status if est else "?"
            style = {"comfortable": "green", "tight": "yellow", "too_large": "red"}.get(fit, "dim")
            tested = vision_cached(m.backend.value, m.name)
            if m.name in getattr(self, "_probing", set()):
                vis = "[yellow]…[/]"
            elif tested is not None:
                vis = "[green]✓[/]" if tested else "[dim]✗v[/]"
            elif m.vision is True:
                vis = "[green]✓[/]"
            elif m.vision is False:
                vis = "[dim]✗v[/]"
            else:
                vis = "[dim]?v[/]"
            th = m.thinking or thinking_support(m.name, m.params_b)
            th_cell = {"levels": "[green]≡[/]", "forced": "[cyan]★[/]",
                       "on-off": "[yellow]◐[/]", "none": "[dim]✗[/]"}.get(th or "", "[dim]?[/]")
            fit_cell = {"comfortable": "✓", "tight": "!", "too_large": "✗"}.get(fit, "?")
            cap_cell = {True: "✓", False: "·", None: "?"}[m.tool_capable]
            sz = f"{human_bytes(m.size_bytes).replace(' ', '')}·{(m.quant or '?')[:4]}" if m.size_bytes else "?"
            vr = human_bytes(est.min_bytes).replace(" ", "") if est else "?"
            star = "★" if (m.name == cur and m.backend.value == cur_be) else ""
            t.add_row(
                set_cell_size(star + m.name, name_w - 1),
                sz,
                f"{m.params_b:g}B" if m.params_b else "?",
                vr,
                f"[{style}]{fit_cell}[/]",
                cap_cell,
                vis,
                th_cell,
            )
            self._rows.append(m)
        if t.row_count:
            # keep the user's selection across refreshes — losing it made
            # actions fire on row 0 (the wrong-model settings bug)
            row = next((i for i, r in enumerate(self._rows) if r is not None and r.name == prev), None)
            if row is None:
                row = next((i for i, r in enumerate(self._rows) if r is not None), 0)
            t.move_cursor(row=row)

    def _selected_name(self) -> str | None:
        m = self._selected()
        return m.name if m else None

    def _selected(self) -> ModelInfo | None:
        try:
            t = self.query_one(DataTable)
        except NoMatches:
            return None
        if not t.row_count or not self._rows:
            return None
        return self._rows[min(t.cursor_coordinate.row, len(self._rows) - 1)]

    # --------------------------------------------------------------- actions
    SORTS = ("name", "size", "params", "quant", "family", "thinking", "vision")

    def action_sort_cycle(self) -> None:
        cur = getattr(self, "_sort", "name")
        self._sort = self.SORTS[(self.SORTS.index(cur) + 1) % len(self.SORTS)]
        if self._sort != "name":
            # sort within each section: decorate, sort, undecorate
            by_be: dict[BackendName, list[ModelInfo]] = {}
            for m in self._models:
                by_be.setdefault(m.backend, []).append(m)
            merged: list[ModelInfo] = []
            for be in _SECTION_ORDER:
                merged.extend(sort_models(by_be.get(be, []), self._sort))
            self._models = merged
        self._populate()
        self._status(f"sorted by [b]{self._sort}[/] (per section)")

    def action_verify_vision(self) -> None:
        m = self._selected()
        if m is None:
            return
        # re-probing is allowed: the disk sidecar can lie (LM Studio may not
        # wire the projector) — only a passed probe is a guarantee
        self._probing = getattr(self, "_probing", set()) | {m.name}
        self._populate()
        self._verify(m.backend.value, m.name)

    @work(exclusive=True, group="vision")
    async def _verify(self, be_name: str, name: str) -> None:
        from ...vision import probe_vision, save_verdict

        be = self.app.svc.backend(be_name)
        self._status(f"👁 probing [b]{be_name}:{name}[/] — loading it and reading two random numbers "
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
        key = m.backend.value
        cfg = self.app.svc.config
        section = getattr(cfg, key, None)
        if section is not None and hasattr(section, "default_model"):
            section.default_model = m.name
            # choosing a model means "use it": switch the default backend too,
            # or a cloud pick would never reach chat
            cfg.default_backend = m.backend
            self.app.svc.save_config()
            self._populate()
            if key == "cloud":
                # a cloud API has no GPU knobs worth asking about — the
                # global generation settings (temp/top-p/max) apply as-is
                self.app.notify(f"default model → cloud:{m.name}")
            elif f"{key}:{m.name}" not in cfg.model_settings:
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
        be = self.app.svc.backend(m.backend.value)
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
                self._pull(be.name.value, name)

        self.run_worker(_go(), exclusive=False)

    @work(exclusive=True)
    async def _pull(self, be_name: str, name: str) -> None:
        be = self.app.svc.backend(be_name)
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
