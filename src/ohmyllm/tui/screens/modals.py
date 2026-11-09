"""Extra modal screens: per-model settings form, tone-of-voice picker."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static, Switch
from textual.widgets._option_list import Option

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

# knobs that only mean something for a local inference server
_LOCAL_ONLY = {"num_gpu", "num_batch", "threads", "kv_cache"}


class ModelSettingsModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, model: ModelInfo) -> None:
        super().__init__(id="model-settings-modal")
        self.model = model

    def _visible_fields(self) -> list[tuple[str, str, type]]:
        if self.model.backend.value == "cloud":
            return [f for f in _FIELDS if f[0] not in _LOCAL_ONLY]
        return list(_FIELDS)

    def compose(self) -> ComposeResult:
        cur = self.app.svc.config.settings_for(self.model.backend, self.model.name)
        fields = self._visible_fields()
        self._fields = fields
        hint = ("cloud API — local GPU knobs don't apply" if fields is not _FIELDS
                and self.model.backend.value == "cloud" else "per model; absolute overrides")
        with Vertical(classes="band", id="ms-band"):
            with Vertical(id="model-settings-box"):
                with VerticalScroll(id="ms-scroll"):
                    yield Label(f"[b]⚙ Settings for {self.model.name[:60]}[/b]  [dim]({hint})[/dim]", classes="ms-title")
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
        for name, label, typ in getattr(self, "_fields", _FIELDS):
            raw = self.query_one(f"#ms-{name}", Input).value.strip()
            if raw == "":
                continue
            try:
                values[name] = typ(raw)
            except ValueError:
                errors.append(f"{label}: bad value {raw!r}")
        if errors:
            box = self.query_one("#model-settings-box", VerticalScroll)
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


_TONE_HELP = [
    ("default", "plain and direct — no persona"),
    ("ruthless", "blunt senior engineer: no flattery, cuts waste, tells you when you're wrong"),
    ("chill", "relaxed and friendly, light humour, still technically exact"),
    ("friendly", "warm and encouraging, plain-spoken"),
    ("professional", "formal, measured, business tone"),
    ("terse", "minimum words: fragments, bullets, zero filler"),
]


class TonePickerModal(ModalScreen[str]):
    """OMP-style submenu: pick a tone with up/down arrows, Enter arms it, Esc closes."""

    BINDINGS = [Binding("escape", "cancel", "Close", priority=True)]
    CSS = """
    TonePickerModal { align: center middle; }
    #tp-box { width: 78; max-height: 90%; border: round $accent; background: $surface; padding: 1 2; }
    #tp-list { height: auto; }
    """

    def __init__(self, current: str, custom: dict[str, str]) -> None:
        super().__init__()
        self._current = current
        self._custom = custom

    def compose(self) -> ComposeResult:

        with Vertical(id="tp-box"):
            yield Static("[b]tone of voice[/b]  [dim]↑↓ move · Enter pick · Esc close[/dim]")
            rows = list(_TONE_HELP)
            for name, frag in sorted(self._custom.items()):
                first = frag.splitlines()[0] if frag else ""
                rows.append((name, (first[:60] + "…") if len(first) > 60 else first or "agent-created tone"))
            opts = []
            for name, desc in rows:
                mark = "▸ " if name == self._current else "  "
                opts.append(Option(f"{mark}[b]{name:<14}[/b][dim]{desc}[/dim]", id=f"tp-{name}"))
            yield OptionList(*opts, id="tp-list")

    @on(OptionList.OptionSelected)
    def _picked(self, event) -> None:
        self.dismiss(str(event.option_id or "").removeprefix("tp-"))

    def action_cancel(self) -> None:
        self.dismiss("")
