"""Backend/server screen: health, start, switch default, instructions."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, Static

from ...config import BackendName

_CARDS = (
    (BackendName.OLLAMA, "ollama"),
    (BackendName.LMSTUDIO, "lmstudio"),
    (BackendName.MOCK, "mock"),
    (BackendName.CLOUD, "cloud"),
)


class BackendScreen(Screen):
    BINDINGS = [Binding("escape", "close", "Back"), Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Static("[b]🖥 Backends & servers[/] — esc back · r refresh", id="screen-title")
        cur = self.app.svc.config.default_backend.value
        with Vertical(id="backend-body", classes="screen-body"):
            for _, key in _CARDS:
                if key == "mock" and cur != "mock":
                    continue  # dev artifact: hidden unless actively in use
                with Vertical(id=f"card-{key}", classes="backend-card"):
                    yield Static("", id=f"text-{key}")
                    with Horizontal(classes="card-buttons"):
                        if key not in ("mock", "cloud"):
                            yield Button("start server", id=f"start-{key}", variant="primary")
                        yield Button("make default", id=f"default-{key}")
                        yield Button("how to connect" if key == "cloud" else "how to set up",
                                     id=f"info-{key}", variant="default")

    def on_mount(self) -> None:
        self.probe()

    def action_close(self) -> None:
        self.app.replace_screen("chat")

    def action_refresh(self) -> None:
        self.probe()

    @work(exclusive=True)
    async def probe(self) -> None:
        app = self.app
        for _, key in _CARDS:
            be = app.svc.backend(key)
            installed = be.is_installed()
            ok, info = await be.health()
            models = "?"
            if ok:
                try:
                    models = len(await be.list_models())
                except Exception:  # noqa: BLE001
                    models = "error"
            dot = "[green]● live[/green]" if ok else "[red]○ down[/red]"
            is_default = app.svc.config.default_backend.value == key
            star = "  ← [b]default[/b]" if is_default else ""
            msg = info.get("error") or info.get("message") or info.get("version") or ""
            if not msg and not ok:
                msg = str(info.get("base_url", ""))
            try:
                tail = f" · {str(msg)[:120]}" if msg else ""
                self.query_one(f"#text-{key}", Static).update(
                    f"{'▣' if is_default else '▢'} [b]{be.display_name}[/b]{star}   {dot}\n"
                    f"   installed: {'yes' if installed else 'no'} · models: {models}{tail}"
                )
            except Exception:  # noqa: BLE001 — card hidden (mock) or screen closed mid-probe
                continue

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = str(event.button.id or "")
        act, _, key = bid.partition("-")
        if act not in {"start", "default", "info"} or not key:
            return
        if act == "start":
            self._start(key)
        elif act == "default":
            self.app.svc.config.default_backend = BackendName(key)
            self.app.svc.save_config()
            self.app._health_probe()
            self.app.notify(f"default backend → {key}")
            self.probe()
        else:
            self._info(key)

    @work(exclusive=True)
    async def _start(self, key: str) -> None:
        be = self.app.svc.backend(key)
        self.app.notify(f"starting {be.display_name}…")
        res = await be.ensure_running()
        if res.ok:
            self.app.backend_ok = True
            self.app.notify(f"{be.display_name}: {res.message}")
        else:
            self.app.notify(f"{be.display_name}: {res.message}", severity="warning")
            self._info(key)
        self.probe()

    def _info(self, key: str) -> None:
        if key == "ollama":
            from ...backends.ollama import INSTALL_INSTRUCTIONS as lines
            title = "Ollama — how to get it running"
        elif key == "lmstudio":
            from ...backends.lmstudio import SERVER_INSTRUCTIONS as lines
            title = "LM Studio — how to start the local server"
        elif key == "cloud":
            cl = self.app.svc.config.cloud
            lines = [
                "Cloud models need an API key — nothing to install.",
                "1. /providers (or the GUI Providers tab)",
                "2. pick a provider → paste the key → u to make it default",
                f"currently active: {cl.provider or 'none'}",
                "env alternative: export OMLLM_KEY_OPENROUTER=sk-or-… (wins over config)",
            ]
            title = "Cloud — hosted models"
        else:
            lines = ["The mock backend needs nothing; it's a scripted local model for development."]
            title = "Mock backend"
        self.app.push_screen(InfoModal(title, lines))


class InfoModal(ModalScreen[None]):
    def __init__(self, title: str, lines: list[str]) -> None:
        super().__init__(id="info-modal")
        self._title = title
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(f"[b]{self._title}[/b]")
            yield Static("\n".join(f"  {i+1}. {ln}" for i, ln in enumerate(self._lines)))
            yield Button("close", id="close-info", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-info":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key in ("escape", "enter", "space"):
            self.dismiss(None)
