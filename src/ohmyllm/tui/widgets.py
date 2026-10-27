"""Custom widgets + modals for the OhMyLLM TUI."""

from __future__ import annotations

import json
from typing import Any

from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..safety import ApprovalOutcome, ApprovalRequest
from ..verbs import verb


class StatusBar(Static):
    """Persistent bottom bar: backend/model/mode/VRAM/tok-s/ctx/save state."""

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self._st: dict[str, Any] = {}

    def render_state(self, st: dict[str, Any]) -> None:
        self._st = st
        self._paint()

    def on_resize(self, event: Any) -> None:
        if self._st:
            self._paint()

    def _paint(self) -> None:
        st = self._st
        w = self.region.width or 120
        dot = "●" if st.get("backend_ok") else "○"
        bstyle = "green" if st.get("backend_ok") else "red"
        mode = str(st.get("mode", "?"))
        mstyle = {
            "strict": "bold cyan",
            "safe": "bold blue",
            "sandboxed": "bold white",
            "dangerous": "bold yellow",
            "full_access": "bold white on red",
        }.get(mode, "bold white")
        vram = st.get("vram", "")
        toks = st.get("tokens_s") or 0
        ctx = st.get("ctx_pct", 0)
        saved = "saved" if st.get("saved") else "unsaved"
        mem = ("🧠 " if w >= 86 else "") + ("on" if st.get("memory", True) else "off")
        eff = st.get("effort", "off")
        ultra = " 🌀ultra" if st.get("ultrathink") and w >= 60 else (
            f" ⚡{eff}" if eff != "off" and w >= 60 else "")
        busy = f" ⚒ {verb()}" if st.get("busy") else ""
        model = str(st.get("model", "—"))[: (34 if w >= 110 else 18)]
        chat = f" · chat {st.get('session', '…')}" if w >= 100 else ""
        mid = f"ctx {ctx}%  {vram}  " if w >= 104 else (f"ctx {ctx}%  " if w >= 70 else "")
        self.update(
            f"[{bstyle}]{dot}[/] {st.get('backend', '?')}  "
            f"[bold]{model}[/]  "
            f"[{mstyle}] {mode} [/]  "
            + mid
            + (f"[magenta]{toks:.1f} tok/s[/]  " if toks and w >= 78 else "")
            + f"[dim]{mem} · {saved}[/]" + chat + f"[magenta]{ultra}[/]"
            + busy
        )



class ModeBanner(Static):
    def set_mode(self, mode: str) -> None:
        if mode == "full_access":
            self.styles.display = "block"
            self.update("[bold white on red]  ⚠ FULL ACCESS — the agent runs tools without asking; everything is still logged[/]")
        elif mode == "dangerous":
            self.styles.display = "block"
            self.update("[bold black on yellow]  ⚠ DANGEROUS MODE — elevated access, confirmations on destructive actions[/]")
        else:
            self.styles.display = "none"


class SepBar(Static):
    """Separator column: a pipe-thin left-eighth block on every row.
    Block glyphs are geometric (they tile at full cell height, unlike
    box-drawing lines), and VS15 thinking emojis keep the grid integer."""

    def render(self) -> Text:
        h = max(1, self.region.height)
        return Text("\n".join("\u258f" for _ in range(h)),
                    style=Style(color="#00e5ff"))


class ToolPanel(Static):
    """Right-hand dock listing recent tool activity + memory events."""

    def __init__(self) -> None:
        super().__init__(id="tool-panel")
        self._lines: list[Text] = []

    def _set_visible(self, visible: bool) -> None:
        self.display = visible
        parent = self.parent
        if parent is not None:
            try:
                parent.query_one("#panel-sep", Static).display = visible
            except Exception:
                pass

    def on_mount(self) -> None:
        self._set_visible(False)  # claim width only when there is something to show

    def add_event(self, ev: dict[str, Any]) -> None:
        self._set_visible(True)
        t = ev.get("type")
        if t in ("tool_plan", "tool_result", "retry", "error"):
            self._settle_previews()
        if t == "tool_plan":
            line = Text()
            line.append(f"plan {ev.get('name')}", style="bold cyan")
            line.append(f"  {ev.get('level')}({ev.get('risk')})", style="cyan")
            line.append("  needs approval" if ev.get("approval") else "  allowed", style="dim")
            if not ev.get("allowed", True):
                line = Text()
                line.append(f"! {ev.get('name')} blocked", style="bold red")
            self._push(line)
        elif t == "tool_result":
            icon = "ok" if ev.get("ok") else "fail"
            line = Text()
            line.append(f"{icon} {ev.get('name')}", style="bold green" if ev.get("ok") else "bold red")
            dur = ev.get("duration_ms")
            if dur:
                line.append(f"  {dur:.0f}ms", style="dim")
            if ev.get("denied"):
                line = Text()
                line.append(f"denied {ev.get('name')}", style="bold yellow")
            self._push(line)
        elif t == "memory_activity":
            line = Text()
            line.append(f"memory {'updated' if ev.get('ok') else 'FAILED'} ({ev.get('tool')})", style="yellow")
            self._push(line)
        elif t == "retry":
            line = Text()
            line.append(f"retry: {str(ev.get('reason'))[:60]}", style="yellow")
            self._push(line)
        elif t == "thinking":
            step_tag = f"(step {ev.get('step', '?')})"
            if self._lines and self._lines[-1].plain.endswith(step_tag):
                return  # retry within the same step: don't stack duplicates
            line = Text()
            line.append(f"💭 {verb()} {step_tag}", style="dim")
            self._push(line)
        elif t == "compaction":
            line = Text()
            line.append(f"compacted {ev.get('before')}->{ev.get('after')} msgs", style="cyan")
            self._push(line)
        elif t == "tool_stream":
            label = f"write {ev.get('name')}"
            txt = Text()
            txt.append(label, style="bold cyan")
            txt.append(f"  {max(1, int(ev.get('chars', 0)) // 1000)}k chars…", style="dim")
            for i in range(len(self._lines) - 1, -1, -1):
                if self._lines[i].plain.startswith(label):
                    self._lines[i] = txt  # refresh in place: keeps its chronological slot
                    break
            else:
                self._lines.append(txt)
            del self._lines[:-12]
            self.update(Text("\n").join(self._lines))
        elif t == "error":
            line = Text()
            line.append(f"! {str(ev.get('message'))[:80]}", style="bold red")
            self._push(line)

    def _settle_previews(self) -> None:
        """A live 'write …' line stops being live once the call resolved."""
        for i, ln in enumerate(self._lines):
            if ln.plain.startswith("write ") and ln.plain.endswith("…"):
                new = Text()
                new.append(ln.plain[:-1], style="bold cyan")
                self._lines[i] = new

    def _push(self, line: Text) -> None:
        self._lines.append(line)
        del self._lines[:-12]
        block = Text("\n").join(self._lines) if self._lines else Text("[dim]no tool activity yet[/dim]", style="dim")
        self.update(block)

    def rotate_thinking(self) -> None:
        """Re-verb the trailing thinking line while a turn runs (1 s tick)."""
        if not self._lines or not self._lines[-1].plain.startswith("💭 "):
            return
        plain = self._lines[-1].plain
        tag = plain[plain.rindex("(step"):] if "(step" in plain else ""
        line = Text()
        line.append(f"💭 {verb()} {tag}".rstrip(), style="dim")
        self._lines[-1] = line
        self.update(Text("\n").join(self._lines))

    def render_header(self, count: int) -> None:  # pragma: no cover - cosmetic
        pass




class PromptModal(ModalScreen[str | None]):
    """Single-line text prompt with optional secret-ish confirm."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, title: str, placeholder: str = "", initial: str = "") -> None:
        super().__init__(id="prompt-modal")
        self.title = title
        self.placeholder = placeholder
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self.title, id="prompt-label")
            yield Input(placeholder=self.placeholder, value=self.initial, id="prompt-input")
            with Horizontal(id="prompt-buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one(Input).value or None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "No", priority=True),
    ]

    def __init__(self, question: str, extra: str = "", ok_label: str = "Yes", danger: bool = False) -> None:
        super().__init__(id="confirm-modal")
        self.question = question
        self.extra = extra
        self.ok_label = ok_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box", classes="danger" if self.danger else ""):
            yield Label(self.question, id="confirm-q")
            if self.extra:
                yield Static(self.extra, id="confirm-extra")
            with Horizontal(id="confirm-buttons"):
                yield Button(self.ok_label, variant="error" if self.danger else "primary", id="yes")
                yield Button("Cancel", variant="default", id="no")

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ApprovalModal(ModalScreen[ApprovalOutcome]):
    """Shows tool, args, risk, mode + preview (diff/command); y/n/a/p keys."""

    BINDINGS = [
        Binding("y", "approve", "Allow"),
        Binding("n", "deny", "Deny"),
        Binding("a", "allow_session", "Allow session"),
        Binding("p", "always", "Permanent rule"),
        Binding("escape", "deny", "Deny", priority=True),
    ]

    def __init__(self, req: ApprovalRequest) -> None:
        super().__init__(id="approval-modal")
        self.req = req

    def compose(self) -> ComposeResult:
        d = self.req.decision
        head = Text.assemble(
            ("⚒ tool: ", "bold"), (self.req.tool, "bold cyan"),
            ("   risk: ", "bold"), (f"{d.level} ({d.risk})", "bold yellow" if d.risk >= 5 else "green"),
            ("   mode: ", "bold"), (str(d.reasons and d.reasons[0] or "").removeprefix("mode="), "dim"),
        )
        with Vertical(id="approval-box"):
            yield Static(head)
            args = json.dumps(self.req.args, ensure_ascii=False, indent=1, default=str)
            if len(args) > 1400:
                args = args[:1400] + "\n…"
            yield Static(Syntax(args, "json", theme="monokai", word_wrap=True), classes="code-block")
            if self.req.preview:
                prev = self.req.preview[:2200]
                lang = "diff" if ("@@" in prev or prev.startswith(("write", "patch"))) else "text"
                yield Static(Syntax(prev, lang if lang != "text" else "text", theme="monokai", word_wrap=True), classes="code-block preview-block")
            with Horizontal(id="approval-buttons"):
                yield Button("Allow once (y)", variant="success", id="ok")
                yield Button("Allow session (a)", variant="primary", id="session")
                yield Button("Permanent rule (p)", variant="warning", id="always")
                yield Button("Deny (n)", variant="error", id="deny")

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self._out(ApprovalOutcome.APPROVED)

    @on(Button.Pressed, "#session")
    def _sess(self) -> None:
        self._out(ApprovalOutcome.APPROVED_SESSION)

    @on(Button.Pressed, "#always")
    def _always(self) -> None:
        self._out(ApprovalOutcome.APPROVED_ALWAYS)

    @on(Button.Pressed, "#deny")
    def _deny(self) -> None:
        self._out(ApprovalOutcome.DENIED)

    def _out(self, o: ApprovalOutcome) -> None:
        self.dismiss(o)

    def action_approve(self) -> None:
        self._out(ApprovalOutcome.APPROVED)

    def action_deny(self) -> None:
        self._out(ApprovalOutcome.DENIED)

    def action_allow_session(self) -> None:
        self._out(ApprovalOutcome.APPROVED_SESSION)

    def action_always(self) -> None:
        self._out(ApprovalOutcome.APPROVED_ALWAYS)
