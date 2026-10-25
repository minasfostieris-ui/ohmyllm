"""Settings screen: every config surface, grouped into labeled sections."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Static, Switch

from ...config import Config


def _field(
    label: str,
    fid: str,
    value: str,
    *,
    hint: str = "",
    placeholder: str = "",
    number: bool = False,
) -> list[Label | Input]:
    """One labeled input: a caption above the box so fields are identifiable
    without focusing each one."""
    text = f"[b]{label}[/b]" + (f"  [dim]{hint}[/dim]" if hint else "")
    return [
        Label(text, classes="st-label"),
        Input(value=value, id=fid, placeholder=placeholder, type="number" if number else "text"),
    ]


def _switch(label: str, sid: str, value: bool, hint: str = "") -> list[Label | Switch]:
    text = f"[b]{label}[/b]" + (f"  [dim]{hint}[/dim]" if hint else "")
    return [Label(text, classes="st-label"), Switch(id=sid, value=value)]


class SettingsScreen(Screen):
    BINDINGS = [Binding("escape", "close", "Back")]

    def compose(self) -> ComposeResult:
        cfg = self.app.svc.config
        with VerticalScroll(id="settings-body", classes="screen-body"):
            yield Static(
                "[b]⚙ Settings[/]  [dim]esc leaves without saving · Save validates everything[/dim]",
                id="screen-title",
            )

            # ------------------------------------------------ backend & model
            with Vertical(classes="st-section"):
                yield Static("Backend & model", classes="st-title")
                yield Static("Which server answers, and which model is the default. "
                             "Ctrl+B switches backend, Ctrl+M manages models.", classes="st-desc")
                yield from _field("Backend", "st-default_backend", cfg.default_backend.value,
                                  hint="ollama | lmstudio | mock")
                yield from _field("Ollama default model", "st-ollama_default",
                                  cfg.ollama.default_model or "",
                                  placeholder="e.g. qwen2.5:7b-instruct")
                yield from _field("LM Studio default model", "st-lms_default",
                                  cfg.lmstudio.default_model or "",
                                  placeholder="e.g. gpt-oss-20b…")

            # ------------------------------------------------------ ollama
            with Vertical(classes="st-section"):
                yield Static("Ollama server", classes="st-title")
                yield from _field("Host", "st-ollama_host", cfg.ollama.host,
                                  hint="where the API listens")
                yield from _field("Binary", "st-ollama_binary", cfg.ollama.binary,
                                  hint="path to the ollama executable")
                with Horizontal(classes="st-row"):
                    with Vertical(classes="st-field"):
                        yield from _switch("Auto-start server", "st-ollama_autostart",
                                           cfg.ollama.auto_start)
                    with Vertical(classes="st-field"):
                        yield from _field("Startup timeout (s)", "st-ollama_timeout",
                                          str(cfg.ollama.startup_timeout), number=True)

            # ------------------------------------------------------ lm studio
            with Vertical(classes="st-section"):
                yield Static("LM Studio server", classes="st-title")
                yield from _field("Base URL", "st-lms_url", cfg.lmstudio.base_url,
                                  hint="http://…:1234")
                yield from _field("App path", "st-lms_binary", cfg.lmstudio.binary_path or "",
                                  hint="(optional) path to LM Studio, enables lms CLI")
                yield from _switch("Auto-start server", "st-lms_autostart",
                                   cfg.lmstudio.auto_start)

            # ----------------------------------------------------- generation
            with Vertical(classes="st-section"):
                yield Static("Generation defaults", classes="st-title")
                yield Static("Applied to every model unless overridden per model "
                             "(Ctrl+M → s).", classes="st-desc")
                with Horizontal(classes="st-row"):
                    with Vertical(classes="st-field"):
                        yield from _field("Temperature", "st-temperature",
                                          str(cfg.generation.temperature), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("Top-p", "st-top_p", str(cfg.generation.top_p), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("Top-k", "st-top_k", str(cfg.generation.top_k), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("Repeat penalty", "st-repeat_penalty",
                                          str(cfg.generation.repeat_penalty), number=True)
                with Horizontal(classes="st-row"):
                    with Vertical(classes="st-field"):
                        yield from _field("Context (tokens)", "st-num_ctx",
                                          str(cfg.generation.num_ctx), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("GPU layers", "st-num_gpu",
                                          str(cfg.generation.num_gpu), number=True,
                                          hint="-1 = all")
                    with Vertical(classes="st-field"):
                        yield from _field("Max reply tokens", "st-max_tokens",
                                          str(cfg.generation.max_tokens), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("Seed", "st-seed", str(cfg.generation.seed),
                                          number=True, hint="0 = random")
                with Horizontal(classes="st-row"):
                    with Vertical(classes="st-field"):
                        yield from _field("Threads", "st-threads", str(cfg.generation.threads),
                                          number=True, hint="0 = auto")
                    with Vertical(classes="st-field"):
                        yield from _field("Request timeout (s)", "st-gen_timeout",
                                          str(cfg.generation.timeout), number=True)
                    with Vertical(classes="st-field"):
                        yield from _switch("Stream tokens", "st-streaming", cfg.generation.streaming)
                    with Vertical(classes="st-field"):
                        yield from _switch("Thinking models", "st-thinking", cfg.generation.thinking,
                                           hint="/think")

            # ---------------------------------------------------------- agent
            with Vertical(classes="st-section"):
                yield Static("Agent runtime", classes="st-title")
                with Horizontal(classes="st-row"):
                    with Vertical(classes="st-field"):
                        yield from _field("Max tool steps per turn", "st-max_steps",
                                          str(cfg.agent.max_steps), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("Tool timeout (s)", "st-tool_timeout",
                                          str(cfg.agent.tool_timeout), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("Native tool calling", "st-native_tools",
                                          cfg.agent.native_tools, hint="auto | force | off")
                    yield from _switch("Stream live thinking", "st-thinking_visible",
                                       cfg.agent.thinking_visible, hint="Ctrl+T in chat")

            # --------------------------------------------------------- memory
            with Vertical(classes="st-section"):
                yield Static("Memory (soul.md)", classes="st-title")
                with Horizontal(classes="st-row"):
                    with Vertical(classes="st-field"):
                        yield from _switch("Enabled", "st-mem_enabled", cfg.memory.enabled)
                    with Vertical(classes="st-field"):
                        yield from _switch("Agent may write", "st-mem_writes", cfg.memory.agent_writes)
                    with Vertical(classes="st-field"):
                        yield from _switch("Explicit /remember only", "st-mem_explicit",
                                           cfg.memory.require_explicit_save)
                with Horizontal(classes="st-row"):
                    with Vertical(classes="st-field"):
                        yield from _field("Max injected chars", "st-mem_inject",
                                          str(cfg.memory.max_inject_chars), number=True)
                    with Vertical(classes="st-field"):
                        yield from _field("Keep N backups", "st-mem_backups",
                                          str(cfg.memory.backups_keep), number=True)

            # --------------------------------------------------------- safety
            with Vertical(classes="st-section"):
                yield Static("Safety", classes="st-title")
                yield from _switch("Workspace writes need confirmation", "st-safety_confirm",
                                   cfg.safety.workspace_write_requires_confirmation)
                yield from _switch("Allow http_fetch tool", "st-safety_http",
                                   cfg.safety.http_fetch_enabled)
                yield from _switch("Catastrophic override (full_access)", "st-safety_cato",
                                   cfg.safety.catastrophic_override,
                                   hint="lets full_access bypass the worst-case block")
                yield from _field("Extra allowed commands", "st-safety_allow",
                                  ", ".join(cfg.safety.command_allowlist_extra),
                                  hint="comma separated", placeholder="e.g. rg, fd")
                yield from _field("Always-denied commands", "st-safety_deny",
                                  ", ".join(cfg.safety.command_denylist),
                                  hint="comma separated")
                yield from _field("Re-allowed sensitive paths", "st-safety_paths",
                                  ", ".join(cfg.safety.allow_sensitive_paths),
                                  hint="comma separated path prefixes")

            # -------------------------------------------------------- guard
            with Vertical(classes="st-section"):
                yield Static("Danger guard (Clef-Flash)", classes="st-title")
                yield Static("A local decision model flags dangerous tool calls even in "
                             "dangerous/full-access modes. Run it CPU+RAM only: "
                             "llama-server -hf ggml-org/Clef-Flash-GGUF -ngl 0 --port 9931",
                             classes="st-desc")
                yield from _switch("Guard enabled", "st-guard_on", cfg.guard.enabled)
                yield from _field("Server URL", "st-guard_url", cfg.guard.base_url)
                yield from _field("Model", "st-guard_model", cfg.guard.model)
                yield from _field("Danger threshold", "st-guard_thr",
                                  str(cfg.guard.danger_threshold), number=True,
                                  hint="0-1; lower = stricter")

            # ------------------------------------------------- workspace & UI
            with Vertical(classes="st-section"):
                yield Static("Workspace & UI", classes="st-title")
                yield from _field("Workspace root", "st-workspace", cfg.workspace or "",
                                  placeholder=f"(auto: {self.app.svc.workspace})",
                                  hint="blank = auto-detect")
                yield from _switch("Confirm before quit", "st-confirm_quit", cfg.ui.confirm_quit)
                yield from _field("Permission mode", "st-mode", cfg.permission_mode.value,
                                  hint="strict | safe | sandboxed | dangerous")
                yield Static("[dim]full_access must be enabled via Ctrl+P with the confirmation phrase[/dim]",
                             classes="st-desc")

            with Horizontal(id="settings-actions"):
                yield Button("Save settings", id="st-save", variant="primary")
                yield Button("Discard & back", id="st-cancel")
            yield Static("", id="st-errors")

    def on_mount(self) -> None:
        pass

    @on(Button.Pressed, "#st-cancel")
    def _cancel(self) -> None:
        self.action_close()

    def action_close(self) -> None:
        self.app.replace_screen("chat")

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
        if be not in {"ollama", "lmstudio", "mock"}:
            errors.append("default_backend must be ollama | lmstudio | mock")
        mode = q("st-mode").value.strip().lower()
        if mode == "full_access":
            mode = None  # must go through Ctrl+P phrase
            errors.append("full_access cannot be set from Settings; use the permission screen phrase")
        elif mode not in {"strict", "safe", "sandboxed", "dangerous"}:
            errors.append("invalid permission mode")
        nt = q("st-native_tools").value.strip().lower()
        if nt not in {"auto", "force", "off"}:
            errors.append("native_tools must be auto|force|off")

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
                "thinking": s("st-thinking"),
            },
            "agent": {
                "max_steps": num(q("st-max_steps"), int),
                "tool_timeout": num(q("st-tool_timeout"), float),
                "native_tools": nt,
                "thinking_visible": s("st-thinking_visible"),
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
            "ui": {"confirm_quit": s("st-confirm_quit")},
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
        self.app.refresh_chrome()
        self.app.notify("settings saved")
        self.action_close()

    def _show_errors(self, errors: list[str]) -> None:
        self.query_one("#st-errors", Static).update("[red]" + "\n".join(f"✖ {e}" for e in errors) + "[/red]")


def _csv(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]
