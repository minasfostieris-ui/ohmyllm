"""Extra modal screens: per-model settings form."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, Switch

from ...backends.base import ModelInfo

_FIELDS = [
    ("temperature", "Temperature 0-2", float),
    ("top_p", "Top-P 0-1", float),
    ("top_k", "Top-K", int),
    ("repeat_penalty", "Repeat penalty", float),
    ("num_ctx", "Context length", int),
    ("num_gpu", "GPU layers (-1=all)", int),
    ("num_batch", "Batch size", int),
    ("max_tokens", "Max tokens", int),
    ("seed", "Seed (0=random)", int),
    ("threads", "Threads (0=auto)", int),
    ("timeout", "Timeout (s)", float),
    ("kv_cache", "K/V cache quant", str),
]


class ModelSettingsModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, model: ModelInfo) -> None:
        super().__init__(id="model-settings-modal")
        self.model = model

    def compose(self) -> ComposeResult:
        cur = self.app.svc.config.settings_for(self.model.backend, self.model.name)
        with Vertical(id="model-settings-box"):
            yield Label(f"[b]⚙ Settings for {self.model.name}[/b]  [dim](stored per model; values are absolute overrides)[/dim]", classes="ms-title")
            fields = list(_FIELDS)
            for i in range(0, len(fields), 3):
                with Horizontal(classes="ms-row"):
                    for name, label, typ in fields[i:i + 3]:
                        with Vertical(classes="ms-field"):
                            yield Label(f"[b]{label}[/b]")
                            yield Input(
                                value=str(getattr(cur, name)),
                                id=f"ms-{name}",
                                type="number" if typ in (int, float) else "text",
                            )
            with Horizontal(classes="ms-row"):
                with Vertical(classes="ms-field"):
                    yield Label("[b]Streaming[/b]")
                    yield Switch(id="ms-streaming", value=cur.streaming)
            with Horizontal(id="ms-buttons"):
                yield Button("Save", variant="primary", id="ms-save")
                yield Button("Cancel", variant="default", id="ms-cancel")

    @on(Button.Pressed, "#ms-save")
    def _save(self) -> None:
        app = self.app
        existing = self.query(Static)
        for w in existing:
            if "bad" in str(getattr(w, "render", "")) or getattr(w, "id", "") == "ms-errors":
                w.remove()
        values: dict[str, object] = {}
        errors: list[str] = []
        for name, label, typ in _FIELDS:
            raw = self.query_one(f"#ms-{name}", Input).value.strip()
            if raw == "":
                continue
            try:
                values[name] = typ(raw)
            except ValueError:
                errors.append(f"{label}: bad value {raw!r}")
        if errors:
            box = self.query_one("#model-settings-box", Vertical)
            if not box.query("#ms-errors"):
                box.mount(Static("[red]" + "; ".join(errors) + "[/red]", id="ms-errors"))
            return
        values["streaming"] = self.query_one("#ms-streaming", Switch).value
        old = app.svc.config.settings_for(self.model.backend, self.model.name)
        merged = old.model_copy(update=values)
        app.svc.config.set_model_settings(self.model.backend, self.model.name, merged)
        app.svc.save_config()
        load_time_changed = merged.num_gpu != old.num_gpu or merged.num_ctx != old.num_ctx
        current = app.svc.current_model()
        if load_time_changed and current == self.model.name:
            app.reload_model(str(self.model.backend.value if hasattr(self.model.backend, "value")
                                else self.model.backend), self.model.name)
        self.dismiss(True)

    @on(Button.Pressed, "#ms-cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)
