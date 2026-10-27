"""First-run wizard: detect → choose → start → models → settings → VRAM → mode → soul → done."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, OptionList, Static, Switch
from textual.widgets.option_list import Option

from ...config import BackendName, PermissionMode
from ...vram import detect_gpus, human_bytes

_STEPS = ["Welcome", "Backends detected", "Server", "Models", "Generation", "VRAM check", "Permission mode", "Memory (soul.md)", "Voice", "Done"]


class WizardScreen(Screen):
    BINDINGS = [Binding("enter", "next", "Next"),
                Binding("ctrl+v", "paste", "Paste clipboard"),
                Binding("escape", "skip", "Skip")]

    def action_paste(self) -> None:
        from ...tui.imagepaste import grab_clip_text

        inp = self.app.focused
        if not isinstance(inp, Input):
            return
        text = grab_clip_text()
        if not text:
            self.app.notify("clipboard has no text", severity="warning", timeout=4)
            return
        start, end = inp.selection
        inp.value = inp.value[:start] + text + inp.value[end:]
        inp.cursor_position = start + len(text)

    def __init__(self) -> None:
        super().__init__()
        self.step = 0
        self.chosen_backend = "ollama"
        self.chosen_model: str | None = None
        self.detected: dict[str, dict] = {}
        self.models: list = []
        self._stt_pick: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="wizard-progress")
        yield Vertical(id="wizard-body")
        with Horizontal(id="wizard-buttons"):
            yield Button("← Back", id="wiz-back", variant="default")
            yield Button("Skip onboarding", id="wiz-skip", variant="default")
            yield Button("Next →", id="wiz-next", variant="primary")

    async def on_mount(self) -> None:
        await self._show_step()
        self._detect()

    # ------------------------------------------------------------- plumbing
    def _title(self) -> str:
        bar = "▮" * (self.step + 1) + "░" * (len(_STEPS) - self.step - 1)
        return f"[b]🐣 OhMyLLM first run[/b]   Step {self.step + 1}/{len(_STEPS)} — {_STEPS[self.step]}   {bar}"

    async def _show_step(self) -> None:
        self.query_one("#wizard-progress", Static).update(self._title())
        body = self.query_one("#wizard-body", Vertical)
        # await the removal: re-mounting a widget with the same ID while the
        # previous one is still registered raises DuplicateIds (worker re-render race)
        await body.remove_children()
        getattr(self, f"_step_{self.step}")(body)
        self.query_one("#wiz-back", Button).disabled = self.step == 0
        self.query_one("#wiz-next", Button).label = "Finish 🚀" if self.step == len(_STEPS) - 1 else "Next →"

    async def action_next(self) -> None:
        await self._advance()

    @on(Button.Pressed, "#wiz-next")
    async def _next_btn(self) -> None:
        await self._advance()

    async def _advance(self) -> None:
        if self.step == len(_STEPS) - 1:
            self._finish()
            return
        self.step = min(self.step + 1, len(_STEPS) - 1)
        await self._show_step()

    def action_skip(self) -> None:
        """Leave the tutorial without applying half-chosen wizard values."""
        cfg = self.app.svc.config
        cfg.first_run_completed = True
        self.app.svc.save_config()
        self.app.notify("onboarding skipped — /onboarding reopens it any time")
        self.app.open_resume_or_new()

    @on(Button.Pressed, "#wiz-skip")
    def _skip_btn(self) -> None:
        self.action_skip()

    @on(Button.Pressed, "#wiz-back")
    async def _back(self) -> None:
        self.step = max(0, self.step - 1)
        await self._show_step()

    # ------------------------------------------------------------ steps
    def _step_0(self, body: Vertical) -> None:
        body.mount(Static(
            "[b]Welcome to OhMyLLM.[/b]\n\n"
            "A local-first agent platform: your models, your GPU, your files, your memory.\n"
            "Nothing leaves this machine unless you explicitly allow a network tool.\n\n"
            "The wizard will find your local model server (Ollama or LM Studio), pick a model,\n"
            "check VRAM fit, set the permission policy, and create your persistent memory file.\n\n"
            "[dim]You can change everything later in Settings (Ctrl+G).[/dim]"
        ))

    def _step_1(self, body: Vertical) -> None:
        body.mount(Static("[b]Detected backends[/b]"))
        rows = []
        for key in ("ollama", "lmstudio", "mock"):
            info = self.detected.get(key, {})
            installed = "yes" if info.get("installed") else "no"
            running = "[green]live[/green]" if info.get("running") else "[red]down[/red]"
            rows.append(f"  {'▣' if key == self.chosen_backend else '▢'} [b]{key:<9}[/b] installed: {installed:<4} server: {running}  {info.get('note','')}")
        if not self.detected:
            rows = ["  detecting…"]
        body.mount(Static("\n".join(rows)))
        ol = OptionList(id="wiz-backend")
        body.mount(ol)
        for key, label in (("ollama", "Ollama — pull & run GGUFs from the CLI"),
                           ("lmstudio", "LM Studio — GUI server, OpenAI-compatible"),
                           ("mock", "Mock backend — no server, explore the UI right now")):
            ol.add_option(Option(f"[b]{label.split(' — ')[0]}[/b] — {label.split(' — ')[1]}", id=key))
        ol.index = ("ollama", "lmstudio", "mock").index(self.chosen_backend)

    def _step_2(self, body: Vertical) -> None:
        body.mount(Static(f"[b]Server status — {self.chosen_backend}[/b]"))
        self._server_line = Static("checking…", id="wiz-server-line")
        body.mount(self._server_line)
        body.mount(Button("start server now", id="wiz-start", variant="primary"))
        if self.chosen_backend == "lmstudio":
            from ...backends.lmstudio import SERVER_INSTRUCTIONS

            body.mount(Static("\n".join(f"  {i+1}. {ln}" for i, ln in enumerate(SERVER_INSTRUCTIONS)), classes="detail-box"))
        elif self.chosen_backend == "ollama":
            from ...backends.ollama import INSTALL_INSTRUCTIONS

            body.mount(Static("\n".join(f"  {i+1}. {ln}" for i, ln in enumerate(INSTALL_INSTRUCTIONS)), classes="detail-box"))
        self._probe_server()

    @work(exclusive=True)
    async def _probe_server(self) -> None:
        try:
            be = self.app.svc.backend(self.chosen_backend)
            ok, info = await be.health()
            self._set_server_line(("live: " + str(info.get("version", info.get("base_url", "ok")))) if ok else "not running — press 'start server now'")
        except Exception as exc:  # noqa: BLE001
            self._set_server_line(f"error: {exc}")

    def _set_server_line(self, text: str) -> None:
        try:
            self.query_one("#wiz-server-line", Static).update(text)
        except Exception:  # noqa: BLE001  (screen may have moved on)
            pass

    @on(Button.Pressed, "#wiz-start")
    def _start_server(self) -> None:
        self._start_worker()

    @work(exclusive=True)
    async def _start_worker(self) -> None:
        be = self.app.svc.backend(self.chosen_backend)
        self._set_server_line("starting…")
        res = await be.ensure_running()
        if res.ok:
            self._set_server_line(f"[green]✔ {res.message}[/green]")
            self.app.backend_ok = True
        else:
            self._set_server_line(f"[yellow]{res.message}[/yellow]\n" + "\n".join(res.instructions[:4]))

    def _step_3(self, body: Vertical) -> None:
        body.mount(Static(f"[b]Models on {self.chosen_backend}[/b]  [dim](enter picks; you can add more later)[/dim]"))
        self._models_line = Static("loading…", id="wiz-models-line")
        body.mount(self._models_line)
        self._model_list = OptionList(id="wiz-models")
        body.mount(self._model_list)
        if self.chosen_backend == "ollama":
            body.mount(Horizontal(
                Input(placeholder="ollama pull name, e.g. qwen2.5:7b-instruct", id="wiz-pull-name"),
                Button("pull", id="wiz-pull"),
            ))
        self._load_models()

    @work(exclusive=True)
    async def _load_models(self) -> None:
        be = self.app.svc.backend(self.chosen_backend)
        try:
            self.models = sorted(await be.list_models(), key=lambda m: m.name)
        except Exception as exc:  # noqa: BLE001
            self.models = []
            try:
                self.query_one("#wiz-models-line", Static).update(f"[red]{exc}[/red]")
            except Exception:  # noqa: BLE001
                pass
        if self.models:
            try:
                ol = self.query_one("#wiz-models", OptionList)
            except Exception:  # noqa: BLE001 — step advanced already
                return
            ol.clear_options()
            for m in self.models:
                star = " [green]★tool-ready[/green]" if m.tool_capable else (" [yellow]text-tools[/yellow]" if m.tool_capable is False else "")
                line = f"[b]{m.name}[/b]  {human_bytes(m.size_bytes) if m.size_bytes else '?'}  {m.quant or '?'}{star}"
                ol.add_option(Option(line, id=m.name))
            if self.chosen_model is None:
                cfg_def = getattr(getattr(self.app.svc.config, self.chosen_backend, None), "default_model", None)
                by_name = {m.name: m for m in self.models}
                if cfg_def and cfg_def in by_name:
                    self.chosen_model = cfg_def
                else:
                    ready = next((m for m in self.models if m.tool_capable), None)
                    self.chosen_model = (ready or self.models[0]).name
            self.query_one("#wiz-models-line", Static).update(f"{len(self.models)} model(s) found")
        else:
            hint = (
                "No models yet. Start the server + pull one (see above), or press Next and switch to the mock backend in Backend screen (Ctrl+B)."
                if self.chosen_backend != "mock" else "mock models unavailable?"
            )
            try:
                self.query_one("#wiz-models-line", Static).update(hint)
            except Exception:  # noqa: BLE001
                pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option is None:
            return
        if event.option.id in {"ollama", "lmstudio", "mock"}:
            self.chosen_backend = str(event.option.id)
            self.chosen_model = None
            self.detected = {}
            self._detect()
        elif str(event.option.id) in {m.value for m in PermissionMode}:
            pass  # mode list; read at finish time
        elif event.option.id:
            self.chosen_model = str(event.option.id)

    @on(Button.Pressed, "#wiz-pull")
    def _pull_btn(self) -> None:
        name = self.query_one("#wiz-pull-name", Input).value.strip()
        if name:
            self._pull(name)

    @work(exclusive=True)
    async def _pull(self, name: str) -> None:
        be = self.app.svc.backend("ollama")
        try:
            await be.pull(name)
            self._set_server_line(f"pulled {name}")
            self._load_models()
        except Exception as exc:  # noqa: BLE001
            self._set_server_line(f"pull failed: {exc}")

    def _step_4(self, body: Vertical) -> None:
        cfg = self.app.svc.config.generation
        body.mount(Static("[b]Default generation settings[/b]  [dim](per-model overrides later in Ctrl+M → s)[/dim]"))
        for fid, label, val in (
            ("wiz-temp", "temperature (0–2)", cfg.temperature),
            ("wiz-ctx", "context length num_ctx", cfg.num_ctx),
            ("wiz-maxtok", "max tokens per answer", cfg.max_tokens),
        ):
            body.mount(Horizontal(
                Label(label.ljust(28)),
                Input(value=str(val), id=fid, type="number"),
            ))
        body.mount(Horizontal(Label("stream tokens   "), Switch(id="wiz-stream", value=cfg.streaming)))

    def _step_5(self, body: Vertical) -> None:
        lines = ["[b]VRAM reality check[/b]"]
        gpus = detect_gpus()
        if gpus:
            for g in gpus:
                lines.append(f"  GPU: {g.model} — {human_bytes(g.total_bytes)} total ({g.source})")
        else:
            lines.append("  [yellow]no GPU detected — CPU inference will be slow for agent work[/yellow]")
        m = next((x for x in self.models if x.name == self.chosen_model), None)
        if m and m.size_bytes:
            gen = self.app.svc.config.generation
            est = m.vram_estimate(gen, sum(g.total_bytes for g in gpus) or None)
            if est:
                lines.append(
                    f"  {m.name}: needs ≈ [b]{human_bytes(est.min_bytes)}[/b] minimum,"
                    f" comfortable ≈ [b]{human_bytes(est.comfortable_bytes)}[/b] at ctx {gen.num_ctx}"
                )
                style = {"comfortable": "green", "tight": "yellow", "too_large": "red", "unknown": "dim"}[est.status]
                lines.append(f"  verdict: [{style}]{est.status}[/{style}]")
                for w in est.warnings:
                    lines.append(f"  [red]⚠ {w}[/red]")
                for n in est.notes[:2]:
                    lines.append(f"  [dim]{n}[/dim]")
        else:
            lines.append("  [dim]select a model on the previous step for a concrete estimate[/dim]")
        lines.append("\n  [dim]estimates are heuristics (weights + KV cache + runtime overhead), not measurements.[/dim]")
        body.mount(Static("\n".join(lines)))

    def _step_6(self, body: Vertical) -> None:
        body.mount(Static("[b]Pick the permission policy[/b]  [dim]enforced by the platform; the model can never change it[/dim]"))
        ol = OptionList(id="wiz-mode")
        self._wizard_modes = [m for m in PermissionMode if m is not PermissionMode.FULL_ACCESS]
        for m in self._wizard_modes:
            color = {PermissionMode.STRICT: "cyan", PermissionMode.SAFE: "blue", PermissionMode.SANDBOXED: "white", PermissionMode.DANGEROUS: "yellow"}[m]
            ol.add_option(Option(f"[{color}]{m.label}[/]  [dim]{m.description[:80]}[/dim]", id=m.value))
        cur = self.app.svc.config.permission_mode
        ol.index = min(self._wizard_modes.index(cur) if cur in self._wizard_modes else len(self._wizard_modes) - 1, len(self._wizard_modes) - 1)
        body.mount(ol)
        body.mount(Static("[dim]full_access is intentionally NOT selectable here — enable it later via Ctrl+P and the confirmation phrase.[/dim]"))


    def _step_7(self, body: Vertical) -> None:
        soul = self.app.svc.soul
        soul.ensure()
        body.mount(Static(
            f"[b]Persistent memory: soul.md[/b]\n"
            f"  location: {soul.path}\n"
            "  This file rides along in every conversation. The agent saves durable facts\n"
            "  (names, projects, preferences, pets) with guarded memory tools — never secrets.\n"
            "  Sections: User · Projects · Preferences · Pets · Lessons · Important Notes."
        ))
        body.mount(Horizontal(Label("agent may write memories   "), Switch(id="wiz-mem", value=True)))
        body.mount(Input(placeholder="optional: something the agent should remember about you right away", id="wiz-seed-memory"))

    def _step_8(self, body: Vertical) -> None:
        from ... import voice as _v

        eng = [e for e, ok in (("sensevoice", _v.find_sensevoice()), ("parakeet", _v.find_parakeet()),
                               ("whisperx", _v.find_whisperx()), ("whisper", _v.find_whisper())) if ok]
        body.mount(Static(
            "[b]🎙 Voice[/b] — talk to OhMyLLM instead of typing, hear replies back\n\n"
            f"  mic recorder: {'✓' if _v.find_recorder() else '✗ (need pw-record/arecord/ffmpeg)'}\n"
            f"  STT runtime:  {'✓ ' + '/'.join(eng) if eng else '✗ (whisper.cpp / whisperx / sensevoice.cpp / parakeet.cpp)'}\n"
            f"  TTS runtime:  {'✓' if _v.find_tts() else '✗ (audio.cpp for Phonon voices)'}\n\n"
            "  [b]Do you want Speech-to-Text?[/] Hold Ctrl+E to talk — the transcript\n"
            "  lands in your input box. Engines by language:\n"
            "    English → Parakeet 0.6B (v3)      EN·KO·ZH·JA·YUE → SenseVoice-Small\n"
            "    ~99 other languages → Whisper + VAD (WhisperX)\n\n"
            "  Pick models any time: [b]/market[/] → Speech-to-text / Text-to-speech.\n"
            "  (Skip this step — nothing is downloaded without you choosing.)\n"
        ))
        row = Horizontal(
            Label("STT model: ".ljust(14)),
            OptionList(
                Option("skip — no speech input", id="vstt-skip"),
                Option("SenseVoice-Small · 254 MB · en/ko/zh/ja/yue", id="vstt-sensevoice"),
                Option("Parakeet TDT 0.6B v3 · 740 MB · best English", id="vstt-parakeet"),
                Option("Whisper small · 488 MB · ~99 languages", id="vstt-whisper"),
                Option("Whisper large-v3-turbo · 574 MB · max accuracy", id="vstt-turbo"),
                id="vstt-list"),
        )
        body.mount(row)
        body.mount(Static(
            "\n  [b]Want TTS?[/] /tts on makes replies spoken through a Phonon voice\n"
            "  (gradium family — en/es/fr/de/pt + multilingual, 22–85 MB each,\n"
            "  pick them in /market → Text-to-speech). Needs audio.cpp installed.\n"
            "  [dim]Tip: /onboarding reopens this tutorial any time.[/dim]"
        ))

    def _step_9(self, body: Vertical) -> None:
        body.mount(Static(
            "[b]All set 🎉[/b]\n\n"
            f"  backend: {self.chosen_backend}   model: {self.chosen_model or '—'}\n"
            f"  mode: {self._mode_choice.value}   memory: {self.app.svc.soul.path}\n\n"
            "  Tips:\n"
            "    Ctrl+K palette · Ctrl+O chats · Ctrl+M models · Ctrl+P permissions\n"
            "    Try:  hello my name is …  →  watch the 🧠 memory indicator save it.\n"
        ))

    # ------------------------------------------------------------ finish
    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        # the step's widgets are unmounted before _finish runs — capture the
        # pick while it happens instead of querying a dead OptionList
        oid = str(event.option_id or "")
        if oid.startswith("vstt-"):
            self._stt_pick = None if oid == "vstt-skip" else oid[5:]

    def _picked_stt(self) -> str | None:
        return getattr(self, "_stt_pick", None)

    def _finish(self) -> None:
        app = self.app
        cfg = app.svc.config
        cfg.default_backend = BackendName(self.chosen_backend)
        section = getattr(cfg, self.chosen_backend, None)
        if section is not None and self.chosen_model:
            section.default_model = self.chosen_model
        try:
            cfg.generation.temperature = float(self._grab("wiz-temp") or cfg.generation.temperature)
            cfg.generation.num_ctx = int(self._grab("wiz-ctx") or cfg.generation.num_ctx)
            cfg.generation.max_tokens = int(self._grab("wiz-maxtok") or cfg.generation.max_tokens)
        except ValueError:
            pass
        try:
            cfg.generation.streaming = self.query_one("#wiz-stream", Switch).value
        except Exception:  # noqa: BLE001
            pass
        try:
            cfg.memory.agent_writes = self.query_one("#wiz-mem", Switch).value
        except Exception:  # noqa: BLE001
            pass
        cfg.permission_mode = self._mode_choice
        cfg.first_run_completed = True
        app.svc.save_config()
        seed = self._grab("wiz-seed-memory")
        if seed:
            app.svc.soul.append("User", seed.strip().rstrip(".") + ".")
        app.open_resume_or_new()
        app.notify("welcome to OhMyLLM — have fun locally 🐣")
        pick = self._picked_stt()
        if pick:
            # schedule on the APP, not this screen: _finish replaces the screen
            # and Textual cancels screen-bound workers on unmount — the old
            # wiring died silently mid-download every time
            self.app.run_worker(_download_stt_app(self.app, pick), exclusive=True, group="wiz-stt")

    def _grab(self, wid: str) -> str:
        try:
            return self.query_one(f"#{wid}", Input).value.strip()
        except Exception:  # noqa: BLE001
            return ""

    @property
    def _mode_choice(self) -> PermissionMode:
        try:
            ol = self.query_one("#wiz-mode", OptionList)
            if ol.index is not None:
                modes = getattr(self, "_wizard_modes", [m for m in PermissionMode if m is not PermissionMode.FULL_ACCESS])
                return modes[min(ol.index, len(modes) - 1)]
        except Exception:  # noqa: BLE001
            pass
        cur = self.app.svc.config.permission_mode
        return cur if cur is not PermissionMode.FULL_ACCESS else PermissionMode.DANGEROUS

    # ------------------------------------------------------------- detect
    @work(exclusive=True)
    async def _detect(self) -> None:
        from ...backends import probe as probe_backends

        result = await probe_backends(self.app.svc.config)
        self.detected = {
            key.value: {
                "installed": v["installed"],
                "running": v["running"],
                "note": (v["health"].get("version") or v["health"].get("base_url") or v["health"].get("error", ""))[:40],
            }
            for key, v in result.items()
        }
        # prefer a running real backend
        if self.step == 1:
            await self._show_step()
        elif not result[BackendName.OLLAMA]["running"] and result[BackendName.LMSTUDIO]["running"]:
            self.chosen_backend = "lmstudio"


_STT_MID = {"sensevoice": "stt-sensevoice", "parakeet": "stt-parakeet",
            "whisper": "stt-whisper-small", "turbo": "stt-whisper-turbo"}


async def _download_stt_app(app, pick: str) -> None:
    from ... import market
    from ...market import DownloadState, resolve

    mid = _STT_MID.get(pick)
    entry = next((e for e in market.CATALOG if e.mid == mid), None)
    if entry is None:
        return
    app.notify(f"🎤 downloading {entry.title} into ~/.ohmyllm/voice …")
    state = DownloadState(entry=entry)
    try:
        await market.download(resolve(entry), state)
    except Exception as exc:  # noqa: BLE001 — worker must surface, never vanish
        state.error = f"{type(exc).__name__}: {exc}"
    if state.error:
        app.notify(f"STT download failed: {state.error}", severity="error", timeout=10)
    else:
        app.notify(f"🎤 {entry.title} ready — hold Ctrl+E in chat to talk", timeout=10)
