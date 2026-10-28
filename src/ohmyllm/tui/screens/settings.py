"""Settings screen: OMP-style tabs across the top, one flat key/value column,
a description line for the focused field. All field ids are mounted at all
times (inactive tabs are display:none) so validation and tests keep working."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Focus
from textual.screen import Screen
from textual.widgets import Button, Input, Label, OptionList, Static, Switch
from textual.widgets._option_list import Option

from ...config import Config
from ..themes import THEME_ORDER, THEMES, resolve_theme

# (key, tab label) — order is the tab bar order
_TABS = [
    ("model", "Model"),
    ("ollama", "Ollama"),
    ("lmstudio", "LM Studio"),
    ("generation", "Generation"),
    ("agent", "Agent"),
    ("memory", "Memory"),
    ("safety", "Safety"),
    ("guard", "Guard"),
    ("ui", "UI"),
]

# id -> description shown in the footer line when the field takes focus
_DESCS: dict[str, str] = {}


def _desc(fid: str, text: str) -> None:
    _DESCS[fid] = text


def _field(
    label: str,
    fid: str,
    value: str,
    *,
    hint: str = "",
    placeholder: str = "",
    number: bool = False,
) -> list[Horizontal]:
    """One flat row: bold label column + input column (OMP key/value style)."""
    _desc(fid, hint or label)
    return [
        Horizontal(
            Label(f"[b]{label}[/b]", classes="st-k"),
            Input(value=value, id=fid, placeholder=placeholder,
                  type="number" if number else "text"),
            classes="st-row2",
        )
    ]


def _switch(label: str, sid: str, value: bool, hint: str = "") -> list[Horizontal]:
    _desc(sid, hint or label)
    return [Horizontal(Label(f"[b]{label}[/b]", classes="st-k"),
                       Switch(id=sid, value=value), classes="st-row2")]


class SettingsScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("left", "prev_tab", "Prev tab", priority=False),
        Binding("right", "next_tab", "Next tab", priority=False),
        # Input swallows up/down before the screen can see them, so these
        # must be priority bindings; the actions delegate to OptionList when
        # the theme picker holds focus (see _move).
        Binding("up", "prev_field", "Previous field", priority=True),
        Binding("down", "next_field", "Next field", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def compose(self) -> ComposeResult:
        cfg = self.app.svc.config
        yield Static(
            "[b]⚙ Settings[/]  [dim]←/→ tabs · ↑/↓ fields · ctrl+s saves · "
            "esc leaves without saving[/dim]",
            id="screen-title",
        )
        with Horizontal(id="settings-tabs"):
            for key, label in _TABS:
                yield Button(label, id=f"st-tab-{key}", classes="st-tab")
        with VerticalScroll(id="settings-body", classes="screen-body"):
            # ------------------------------------------------------ Model tab
            with Vertical(id="st-page-model", classes="st-page -show"):
                yield Static("Which server answers, and which model is the default. "
                             "Ctrl+B switches backend, Ctrl+M manages models.",
                             classes="st-desc")
                yield from _field("Backend", "st-default_backend", cfg.default_backend.value,
                                  hint="ollama | lmstudio | cloud | mock")
                yield from _field("Ollama default model", "st-ollama_default",
                                  cfg.ollama.default_model or "",
                                  placeholder="e.g. qwen2.5:7b-instruct",
                                  hint="model ollama uses for new chats")
                yield from _field("LM Studio default model", "st-lms_default",
                                  cfg.lmstudio.default_model or "",
                                  placeholder="e.g. gpt-oss-20b…",
                                  hint="model LM Studio uses for new chats")

            # ----------------------------------------------------- Ollama tab
            with Vertical(id="st-page-ollama", classes="st-page"):
                yield from _field("Host", "st-ollama_host", cfg.ollama.host,
                                  hint="where the ollama API listens")
                yield from _field("Binary", "st-ollama_binary", cfg.ollama.binary,
                                  hint="path to the ollama executable")
                yield from _switch("Auto-start server", "st-ollama_autostart",
                                   cfg.ollama.auto_start,
                                   hint="start ollama serve when it is not running")
                yield from _field("Startup timeout (s)", "st-ollama_timeout",
                                  str(cfg.ollama.startup_timeout), number=True,
                                  hint="seconds to wait for the server")

            # --------------------------------------------------- LM Studio tab
            with Vertical(id="st-page-lmstudio", classes="st-page"):
                yield from _field("Base URL", "st-lms_url", cfg.lmstudio.base_url,
                                  hint="http://…:1234")
                yield from _field("App path", "st-lms_binary",
                                  cfg.lmstudio.binary_path or "",
                                  hint="(optional) path to LM Studio, enables lms CLI")
                yield from _switch("Auto-start server", "st-lms_autostart",
                                   cfg.lmstudio.auto_start,
                                   hint="launch LM Studio when it is not running")

            # ------------------------------------------------- Generation tab
            with Vertical(id="st-page-generation", classes="st-page"):
                yield Static("Applied to every model unless overridden per model "
                             "(Ctrl+M → s).", classes="st-desc")
                yield from _field("Temperature", "st-temperature",
                                  str(cfg.generation.temperature), number=True,
                                  hint="0-2; lower = more deterministic")
                yield from _field("Top-p", "st-top_p", str(cfg.generation.top_p),
                                  number=True, hint="nucleus sampling 0-1")
                yield from _field("Top-k", "st-top_k", str(cfg.generation.top_k),
                                  number=True, hint="sample from the k best tokens")
                yield from _field("Repeat penalty", "st-repeat_penalty",
                                  str(cfg.generation.repeat_penalty), number=True,
                                  hint="discourage repeating tokens")
                yield from _field("Context (tokens)", "st-num_ctx",
                                  str(cfg.generation.num_ctx), number=True,
                                  hint="prompt + reply window")
                yield from _field("GPU layers", "st-num_gpu",
                                  str(cfg.generation.num_gpu), number=True,
                                  hint="-1 = offload every layer to GPU")
                yield from _field("Max reply tokens", "st-max_tokens",
                                  str(cfg.generation.max_tokens), number=True,
                                  hint="cap on one assistant reply")
                yield from _field("Seed", "st-seed", str(cfg.generation.seed),
                                  number=True, hint="0 = random each run")
                yield from _field("Threads", "st-threads", str(cfg.generation.threads),
                                  number=True, hint="0 = auto")
                yield from _field("Request timeout (s)", "st-gen_timeout",
                                  str(cfg.generation.timeout), number=True,
                                  hint="seconds before a request fails")
                yield from _switch("Stream tokens", "st-streaming",
                                   cfg.generation.streaming,
                                   hint="show tokens as they are generated")
                yield from _field("Reasoning effort", "st-effort", cfg.generation.effort,
                                  hint="off|low|medium|high|xhigh|max")
                yield from _field("K/V cache quant", "st-kv", cfg.generation.kv_cache,
                                  hint="f16|q8_0|q4_1|fp8 — smaller = less VRAM")

            # ----------------------------------------------------- Agent tab
            with Vertical(id="st-page-agent", classes="st-page"):
                yield from _field("Max tool steps per turn", "st-max_steps",
                                  str(cfg.agent.max_steps), number=True,
                                  hint="how many tool calls one turn may take")
                yield from _field("Tool timeout (s)", "st-tool_timeout",
                                  str(cfg.agent.tool_timeout), number=True,
                                  hint="seconds per tool call")
                yield from _field("Native tool calling", "st-native_tools",
                                  cfg.agent.native_tools, hint="auto | force | off")
                yield from _switch("Stream live thinking", "st-thinking_visible",
                                   cfg.agent.thinking_visible,
                                   hint="show the thinking panel in chat (Ctrl+T)")
                yield from _switch("Relentless agent loop", "st-agentloop",
                                   cfg.agent.agent_loop,
                                   hint="/agentloop: never stop until the result is verified perfect")

            # ---------------------------------------------------- Memory tab
            with Vertical(id="st-page-memory", classes="st-page"):
                yield from _switch("Enabled", "st-mem_enabled", cfg.memory.enabled,
                                   hint="inject soul.md memory into prompts")
                yield from _switch("Agent may write", "st-mem_writes",
                                   cfg.memory.agent_writes,
                                   hint="let the agent save memories to soul.md")
                yield from _switch("Explicit /remember only", "st-mem_explicit",
                                   cfg.memory.require_explicit_save,
                                   hint="require an explicit user request to save")
                yield from _field("Max injected chars", "st-mem_inject",
                                  str(cfg.memory.max_inject_chars), number=True,
                                  hint="budget for memory text in the prompt")
                yield from _field("Keep N backups", "st-mem_backups",
                                  str(cfg.memory.backups_keep), number=True,
                                  hint="soul.md backups kept on disk")

            # ---------------------------------------------------- Safety tab
            with Vertical(id="st-page-safety", classes="st-page"):
                yield from _switch("Workspace writes need confirmation",
                                   "st-safety_confirm",
                                   cfg.safety.workspace_write_requires_confirmation,
                                   hint="ask before writing outside the plan")
                yield from _switch("Allow http_fetch tool", "st-safety_http",
                                   cfg.safety.http_fetch_enabled,
                                   hint="let the agent fetch URLs")
                yield from _switch("Catastrophic override (full_access)",
                                   "st-safety_cato", cfg.safety.catastrophic_override,
                                   hint="lets full_access bypass the worst-case block")
                yield from _field("Extra allowed commands", "st-safety_allow",
                                  ", ".join(cfg.safety.command_allowlist_extra),
                                  hint="comma separated; e.g. rg, fd",
                                  placeholder="e.g. rg, fd")
                yield from _field("Always-denied commands", "st-safety_deny",
                                  ", ".join(cfg.safety.command_denylist),
                                  hint="comma separated")
                yield from _field("Re-allowed sensitive paths", "st-safety_paths",
                                  ", ".join(cfg.safety.allow_sensitive_paths),
                                  hint="comma separated path prefixes")

            # ----------------------------------------------------- Guard tab
            with Vertical(id="st-page-guard", classes="st-page"):
                yield Static("A local decision model flags dangerous tool calls even in "
                             "dangerous/full-access modes. Run it CPU+RAM only: "
                             "llama-server -hf ggml-org/Clef-Flash-GGUF -ngl 0 --port 9931",
                             classes="st-desc")
                yield from _switch("Guard enabled", "st-guard_on", cfg.guard.enabled,
                                   hint="consult the local danger model")
                yield from _field("Server URL", "st-guard_url", cfg.guard.base_url,
                                  hint="where llama-server listens")
                yield from _field("Model", "st-guard_model", cfg.guard.model,
                                  hint="guard model name")
                yield from _field("Danger threshold", "st-guard_thr",
                                  str(cfg.guard.danger_threshold), number=True,
                                  hint="0-1; lower = stricter")

            # -------------------------------------------------------- UI tab
            with Vertical(id="st-page-ui", classes="st-page"):
                yield from _field("Workspace root", "st-workspace", cfg.workspace or "",
                                  placeholder=f"(auto: {self.app.svc.workspace})",
                                  hint="blank = auto-detect")
                yield from _switch("Confirm before quit", "st-confirm_quit",
                                   cfg.ui.confirm_quit,
                                   hint="ask before Ctrl+Q leaves")
                yield Horizontal(Label("[b]Theme[/b]", classes="st-k"),
                                 Static("[dim]highlight to preview live · Save keeps it[/dim]",
                                        classes="st-inline-hint"), classes="st-row2")
                yield OptionList(*[Option(n, id=n) for n in THEME_ORDER],
                                 id="st-theme-list")
                yield from _field("Permission mode", "st-mode", cfg.permission_mode.value,
                                  hint="strict | safe | sandboxed | dangerous")
                yield Static("[dim]full_access must be enabled via Ctrl+P with the "
                             "confirmation phrase[/dim]", classes="st-desc")

        with Vertical(id="settings-footer"):
            yield Static("", id="st-desc-focused")
            yield Static("", id="st-errors")
            with Horizontal(id="settings-actions"):
                yield Button("Save settings", id="st-save", variant="primary")
                yield Button("Discard & back", id="st-cancel")

    def on_mount(self) -> None:
        cur = resolve_theme(self.app.svc.config.ui.theme)
        self.query_one("#st-theme-list", OptionList).highlighted = THEME_ORDER.index(cur)
        self._activate_tab(0)

    # ------------------------------------------------------------- tab logic
    def _tab_keys(self) -> list[str]:
        return [k for k, _ in _TABS]

    def _activate_tab(self, idx: int) -> None:
        keys = self._tab_keys()
        idx = max(0, min(idx, len(keys) - 1))
        self._tab_idx = idx
        for i, (key, _label) in enumerate(_TABS):
            btn = self.query_one(f"#st-tab-{key}", Button)
            page = self.query_one(f"#st-page-{key}")
            btn.set_class(i == idx, "-active")
            page.set_class(i == idx, "-show")

    @on(Button.Pressed, ".st-tab")
    def _tab_pressed(self, event: Button.Pressed) -> None:
        key = event.button.id.removeprefix("st-tab-")
        self._activate_tab(self._tab_keys().index(key))

    def action_next_tab(self) -> None:
        self._activate_tab(getattr(self, "_tab_idx", 0) + 1)

    def action_prev_tab(self) -> None:
        self._activate_tab(getattr(self, "_tab_idx", 0) - 1)
    # ------------------------------------------------- classical field walk
    def _fields(self) -> list:
        page = self.query_one(f"#st-page-{_TABS[getattr(self, '_tab_idx', 0)][0]}")
        out = [w for w in page.query("Input, Switch, OptionList") if w.focusable]
        out += [self.query_one("#st-save"), self.query_one("#st-cancel")]
        return out

    def _move(self, step: int) -> None:
        f = self.focused
        if isinstance(f, OptionList):
            # arrows belong to the list while it is focused — live preview
            if step > 0:
                f.action_cursor_down()
            else:
                f.action_cursor_up()
            return
        fields = self._fields()
        if not fields:
            return
        try:
            i = fields.index(self.focused)
        except ValueError:
            i = 0 if step > 0 else len(fields) - 1
        else:
            i = (i + step) % len(fields)
        fields[i].focus()

    def action_next_field(self) -> None:
        self._move(1)

    def action_prev_field(self) -> None:
        self._move(-1)

    # ------------------------------------------------- whole-row click targets
    @on(Click)
    def _row_click(self, event: Click) -> None:
        # clicking the label column focuses (or toggles) the field beside it —
        # classical TUIs let you hit the whole row, not a 7-cell switch
        w = event.control
        if isinstance(w, (Label, Static)) and w.parent is not None:
            sw = w.parent.query(Switch)
            if sw:
                sw.first().toggle()
                return
            inp = w.parent.query(Input)
            if inp:
                inp.first().focus()

    # --------------------------------------------------------- focus helper
    @on(Focus)
    def _focus_gained(self, event: Focus) -> None:
        wid = getattr(event.sender, "id", None)
        if isinstance(event.sender, (Input, Switch)) and wid:
            self._show_desc(wid)

    # ---------------------------------------------------------- theme picker
    @on(OptionList.OptionHighlighted, "#st-theme-list")
    def _theme_preview(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id and event.option_id in THEMES:
            self.app.theme = event.option_id

    @on(OptionList.OptionSelected, "#st-theme-list")
    def _theme_pick(self, event: OptionList.OptionSelected) -> None:
        # click / Enter — OptionHighlighted only fires on mouse hover, so
        # without this the picker looked dead for keyboard and mouse users
        if event.option_id and event.option_id in THEMES:
            self.app.theme = event.option_id

    def _selected_theme(self) -> str:
        tl = self.query_one("#st-theme-list", OptionList)
        try:
            opt = tl.get_option_at_index(tl.highlighted) if tl.highlighted is not None else None
        except Exception:  # noqa: BLE001
            opt = None
        return opt.id if opt and opt.id in THEMES else resolve_theme(self.app.svc.config.ui.theme)

    @on(Button.Pressed, "#st-cancel")
    def _cancel(self) -> None:
        self.action_close()

    def action_close(self) -> None:
        if not getattr(self, "_committed", False):
            # esc / discard: undo the live theme preview
            self.app.theme = resolve_theme(self.app.svc.config.ui.theme)
        self.app.replace_screen("chat")

    def action_save(self) -> None:
        self._save()

    @on(Button.Pressed, "#st-save")
    def _save(self) -> None:
        errors: list[str] = []
        q = lambda i: self.query_one(f"#{i}", Input)  # noqa: E731
        s = lambda i: self.query_one(f"#{i}", Switch).value  # noqa: E731

        def num(inp, cast):
            raw = inp.value.strip()
            try:
                return cast(raw)
            except ValueError:
                errors.append(f"{inp.id}: invalid number {raw!r}")
                return None

        be = q("st-default_backend").value.strip().lower()
        if be not in {"ollama", "lmstudio", "cloud", "mock"}:
            errors.append("st-default_backend: must be ollama | lmstudio | cloud | mock")
        mode = q("st-mode").value.strip().lower()
        if mode == "full_access":
            mode = None  # must go through Ctrl+P phrase
            errors.append("st-mode: full_access cannot be set from Settings; "
                          "use the permission screen phrase")
        elif mode not in {"strict", "safe", "sandboxed", "dangerous"}:
            errors.append("st-mode: invalid permission mode")
        nt = q("st-native_tools").value.strip().lower()
        if nt not in {"auto", "force", "off"}:
            errors.append("st-native_tools: must be auto|force|off")

        payload: dict = {
            "version": 1,
            "default_backend": be,
            "permission_mode": mode or "sandboxed",
            "first_run_completed": True,
            "workspace": q("st-workspace").value.strip() or None,
            "ollama": {
                "host": q("st-ollama_host").value.strip(),
                "binary": q("st-ollama_binary").value.strip() or "ollama",
                "auto_start": s("st-ollama_autostart"),
                "startup_timeout": num(q("st-ollama_timeout"), float) or 30.0,
                "default_model": q("st-ollama_default").value.strip() or None,
            },
            "lmstudio": {
                "base_url": q("st-lms_url").value.strip(),
                "auto_start": s("st-lms_autostart"),
                "startup_timeout": 30.0,
                "default_model": q("st-lms_default").value.strip() or None,
                "binary_path": q("st-lms_binary").value.strip() or None,
            },
            "generation": {
                "temperature": num(q("st-temperature"), float),
                "top_p": num(q("st-top_p"), float),
                "top_k": num(q("st-top_k"), int),
                "repeat_penalty": num(q("st-repeat_penalty"), float),
                "num_ctx": num(q("st-num_ctx"), int),
                "num_gpu": num(q("st-num_gpu"), int),
                "max_tokens": num(q("st-max_tokens"), int),
                "seed": num(q("st-seed"), int),
                "threads": num(q("st-threads"), int),
                "timeout": num(q("st-gen_timeout"), float),
                "streaming": s("st-streaming"),
                "effort": (q("st-effort").value.strip().lower() or "off"),
                "kv_cache": (q("st-kv").value.strip().lower() or "q4_1"),
            },
            "agent": {
                "max_steps": num(q("st-max_steps"), int),
                "tool_timeout": num(q("st-tool_timeout"), float),
                "native_tools": nt,
                "thinking_visible": s("st-thinking_visible"),
                "agent_loop": s("st-agentloop"),
            },
            "memory": {
                "enabled": s("st-mem_enabled"),
                "agent_writes": s("st-mem_writes"),
                "require_explicit_save": s("st-mem_explicit"),
                "max_inject_chars": num(q("st-mem_inject"), int),
                "backups_keep": num(q("st-mem_backups"), int),
            },
            "safety": {
                "workspace_write_requires_confirmation": s("st-safety_confirm"),
                "http_fetch_enabled": s("st-safety_http"),
                "catastrophic_override": s("st-safety_cato"),
                "command_allowlist_extra": _csv(q("st-safety_allow").value),
                "command_denylist": _csv(q("st-safety_deny").value),
                "allow_sensitive_paths": _csv(q("st-safety_paths").value),
            },
            "guard": {
                "enabled": s("st-guard_on"),
                "base_url": q("st-guard_url").value.strip() or "http://127.0.0.1:9931",
                "model": q("st-guard_model").value.strip() or "clef-flash",
                "danger_threshold": num(q("st-guard_thr"), float),
            },
            "ui": {"confirm_quit": s("st-confirm_quit"), "theme": self._selected_theme()},
            "model_settings": self.app.svc.config.model_settings,
        }
        if errors:
            self._show_errors(errors)
            return
        try:
            new_cfg = Config.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            self._show_errors([str(exc)[:1500]])
            return
        self.app.svc.apply_config(new_cfg)
        self.app.theme = self._selected_theme()
        self._committed = True
        self.app.refresh_chrome()
        self.app.notify("settings saved")
        self.action_close()

    def _show_errors(self, errors: list[str]) -> None:
        self.query_one("#st-errors", Static).update(
            "[red]" + "\n".join(f"✖ {e}" for e in errors) + "[/red]")
        # the offending field may live on a hidden tab — jump there so the
        # user sees what the complaint is about instead of a cryptic id
        import re

        m = re.search(r"st-[a-z_]+", " ".join(errors))
        if not m:
            return
        fid = m.group(0)
        for i, (key, _label) in enumerate(_TABS):
            page = self.query_one(f"#st-page-{key}")
            found = page.query(f"#{fid}")
            if found:
                self._activate_tab(i)
                found.first().focus()
                return


def _csv(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]
