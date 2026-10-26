"""Providers screen: connect cloud API keys (OpenRouter, Anthropic, …).

The answer to "where do I put my API keys?" — here, or the Providers tab in
the GUI, or OMLLM_KEY_<PROVIDER_ID> in the environment. Keys are stored in
config.toml (owner-only) and only ever displayed masked.
"""
from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ...providers import PROVIDERS, key_for, mask


class KeyModal(ModalScreen[str | None]):
    """Paste-your-key dialog; empty + enter removes the stored key."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, current: str, secret: bool = True) -> None:
        super().__init__()
        self.title = title
        self.current = current
        self.secret = secret

    def compose(self) -> ComposeResult:
        hint = f"stored: {mask(self.current)} (enter replaces it, empty removes it)" if self.current \
            else "paste the key and press enter — leave empty to cancel"
        with Vertical(id="key-modal", classes="modal-card"):
            yield Static(f"[b]{self.title}[/]", id="key-title")
            yield Static(hint, classes="modal-hint")
            yield Input(password=self.secret, placeholder="sk-…", id="key-input")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", variant="primary", id="key-save")
                yield Button("Cancel", id="key-cancel")

    @on(Button.Pressed, "#key-save")
    def _save(self) -> None:
        self.dismiss(self.query_one("#key-input", Input).value.strip())

    @on(Button.Pressed, "#key-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProvidersScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("enter", "connect", "Connect / change key", priority=True),
        Binding("u", "use", "Use as default backend"),
        Binding("x", "forget", "Remove key"),
        Binding("c", "custom", "Add custom OpenAI-compatible"),
        Binding("e", "edit", "Edit base/models of highlighted custom"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]🔑 Providers[/] — cloud models via API keys · enter connect · u use · x remove · c custom",
            id="screen-title",
        )
        yield OptionList(id="prov-list")
        yield Static("", id="prov-detail")

    def on_mount(self) -> None:
        self.refresh_list()

    def action_close(self) -> None:
        self.app.replace_screen("chat")

    def action_refresh(self) -> None:
        self.refresh_list()

    # ------------------------------------------------------------------ list
    def _rows(self) -> list[tuple[str, str, str, str]]:
        """(pid, line, group, detail) — grouped Popular → Other → Custom."""
        cl = self.app.svc.config.cloud
        rows: dict[str, list[tuple[str, str, str, str]]] = {"Popular": [], "Other": [], "Custom": []}
        for p in PROVIDERS:
            key = key_for(p.pid, cl.keys)
            state = f"[green]✓ {mask(key)}[/green]" if key else "[dim]no key[/dim]"
            active = "  [b][cyan]← active[/b][/cyan]" if cl.provider == p.pid and key else ""
            star = " [dim](recommended)[/dim]" if p.recommended else ""
            line = f"{p.name:<16} {state}{active}   [dim]{p.blurb}[/dim]{star}"
            detail = (f"[b]{p.name}[/]  [dim]{p.kind} · {p.base_url}[/dim]\n"
                      f"  {p.blurb}\n"
                      f"  get a key: {p.signup_url or '(custom endpoint)'}\n"
                      f"  [dim]enter connect · u use as default backend · x remove key[/dim]")
            rows["Popular" if p.popular else "Other"].append((p.pid, line, "Popular" if p.popular else "Other", detail))
        for pid, name in sorted(cl.customs.items()):
            key = key_for(pid, cl.keys)
            state = f"[green]✓ {mask(key)}[/green]" if key else "[dim]no key[/dim]"
            active = "  [b][cyan]← active[/b][/cyan]" if cl.provider == pid and key else ""
            base = cl.bases.get(pid, "")
            ml = cl.model_lists.get(pid, "")
            line = f"{name:<16} {state}{active}   [dim]custom · {base}{' · ' + ml if ml else ''}[/dim]"
            detail = (f"[b]{name}[/]  [dim]openai · {base}[/dim]\n"
                      f"  models: {ml or '(listed from the endpoint via /models)'}\n"
                      "  your own OpenAI-compatible endpoint (vLLM, Ollama-cloud, LAN box…)\n"
                      "  [dim]enter connect · u use · x remove key · e edit base/models[/dim]")
            rows["Custom"].append((pid, line, "Custom", detail))
        return rows["Popular"] + rows["Other"] + rows["Custom"]

    def refresh_list(self) -> None:
        lst = self.query_one(OptionList)
        lst.clear_options()
        rows = self._rows()
        self._details = {f"p-{pid}": detail for pid, _, _, detail in rows}
        last_group = ""
        for pid, line, group, _detail in rows:
            if group != last_group:
                lst.add_option(Option("─" * 58, id=f"r-{group}-{len(lst.options)}", disabled=True))
                lst.add_option(Option(f"[b]{group}[/]", id=f"h-{group}", disabled=True))
                last_group = group
            lst.add_option(Option(line, id=f"p-{pid}"))
        self._paint_detail()

    def on_option_list_option_highlight(self, event: OptionList.OptionHighlighted) -> None:
        self._paint_detail()

    def _paint_detail(self) -> None:
        try:
            lst = self.query_one(OptionList)
            hid = lst.highlighted
            if hid is None:
                return
            opt = lst.get_option_at_index(hid)
            detail = getattr(self, "_details", {}).get(str(opt.id), "")
            self.query_one("#prov-detail", Static).update(detail)
        except Exception:  # noqa: BLE001
            pass

    def _pid(self) -> str | None:
        lst = self.query_one(OptionList)
        hid = lst.highlighted
        if hid is None:
            return None
        opt_id = str(lst.get_option_at_index(hid).id or "")
        return opt_id[2:] if opt_id.startswith("p-") else None

    # --------------------------------------------------------------- actions
    def action_connect(self) -> None:
        pid = self._pid()
        if pid is None:
            return
        cl = self.app.svc.config.cloud
        label = self._label(pid)
        self.app.push_screen(KeyModal(f"🔑 {label} API key", key_for(pid, cl.keys)),
                             lambda val: self._store(pid, val))

    def _store(self, pid: str, val: str | None) -> None:
        if val is None:
            return
        cl = self.app.svc.config.cloud
        if val:
            cl.keys[pid] = val
        else:
            cl.keys.pop(pid, None)
        self.app.svc.save_config()
        self.refresh_list()
        if val:
            self._test(pid)

    @work(exclusive=True)
    async def _test(self, pid: str) -> None:
        # health-check on a deep copy: swapping the live config.provider here
        # raced with action_use on slow runners and silently reverted the pick
        from ...backends import make_backend

        cfg = self.app.svc.config.model_copy(deep=True)
        cfg.cloud.provider = pid
        try:
            ok, info = await make_backend("cloud", cfg).health()
            if ok:
                self.app.notify(f"🔑 {self._label(pid)}: live — {info.get('models', '?')} models", timeout=8)
            else:
                self.app.notify(f"{self._label(pid)}: {info.get('error', 'unreachable')}",
                                severity="warning", timeout=10)
        except Exception as exc:  # noqa: BLE001 — never let the ping kill the screen
            self.app.notify(f"{self._label(pid)}: {type(exc).__name__}: {exc}",
                            severity="warning", timeout=10)

    def action_use(self) -> None:
        pid = self._pid()
        if pid is None:
            return
        cl = self.app.svc.config.cloud
        if not key_for(pid, cl.keys):
            self.app.notify("connect a key first (enter)", severity="warning")
            return
        cl.provider = pid
        from ...config import BackendName

        self.app.svc.config.default_backend = BackendName.CLOUD
        self.app.svc.save_config()
        self.refresh_list()
        self.app.notify(f"☁ {self._label(pid)} is now the default backend — Ctrl+M picks its models")

    def action_forget(self) -> None:
        pid = self._pid()
        if pid is None:
            return
        cl = self.app.svc.config.cloud
        cl.keys.pop(pid, None)
        deselected = ""
        if cl.provider == pid:
            cl.provider = ""
            deselected = " — provider deselected (it had no key)"
        self.app.svc.save_config()
        self.refresh_list()
        self.app.notify(f"removed the stored key for {self._label(pid)}{deselected}")

    def action_custom(self) -> None:
        self.app.push_screen(KeyModal(
            "custom provider — Name|https://host/v1|model-a,model-b",
            "the last part (model ids, comma-separated) is optional — needed when the "
            "endpoint has no /models listing; edit later with e", secret=False),
            lambda val: self._add_custom(val))

    def _add_custom(self, val: str | None) -> None:
        if not val:
            return
        parts = [x.strip() for x in val.split("|")]
        name = parts[0] or (parts[1] if len(parts) > 1 else "") or "Custom"
        base = parts[1] if len(parts) > 1 else ""
        models = parts[2] if len(parts) > 2 else ""
        from urllib.parse import urlparse

        pr = urlparse(base)
        if pr.scheme not in ("http", "https") or not pr.netloc:
            self.app.notify("usage: Name|https://my-host/v1|model-a,model-b", severity="warning")
            return
        import uuid

        pid = f"custom-{uuid.uuid4().hex[:8]}"
        cl = self.app.svc.config.cloud
        cl.customs[pid] = name
        base = base.rstrip("/")
        if urlparse(base).path in ("", "/"):
            base += "/v1"
        cl.bases[pid] = base
        if models:
            cl.model_lists[pid] = models
        self.app.svc.save_config()
        self.refresh_list()
        self.app.notify(f"added {name} — now enter → paste its key")

    def action_edit(self) -> None:
        pid = self._pid()
        if pid is None:
            return
        cl = self.app.svc.config.cloud
        if pid not in cl.customs:
            self.app.notify("only custom providers are editable — registry URLs are fixed", severity="warning")
            return
        current = f"{cl.customs[pid]}|{cl.bases.get(pid, '')}|{cl.model_lists.get(pid, '')}"
        self.app.push_screen(KeyModal(f"edit {cl.customs[pid]} — Name|URL|models", current, secret=False),
                             lambda val: self._apply_edit(pid, val))

    def _apply_edit(self, pid: str, val: str | None) -> None:
        if not val:
            return
        from urllib.parse import urlparse

        parts = [x.strip() for x in val.split("|")]
        cl = self.app.svc.config.cloud
        if parts and parts[0]:
            cl.customs[pid] = parts[0]
        if len(parts) > 1 and urlparse(parts[1]).scheme in ("http", "https"):
            base = parts[1].rstrip("/")
            if urlparse(base).path in ("", "/"):
                base += "/v1"
            cl.bases[pid] = base
        if len(parts) > 2:
            if parts[2]:
                cl.model_lists[pid] = parts[2]
            else:
                cl.model_lists.pop(pid, None)
        self.app.svc.save_config()
        self.refresh_list()
        self.app.notify(f"updated {cl.customs[pid]}")

    def _label(self, pid: str) -> str:
        from ...providers import get

        p = get(pid)
        if p:
            return p.name
        return self.app.svc.config.cloud.customs.get(pid, pid)
