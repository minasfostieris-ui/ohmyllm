"""`ohmyllm --plain` — full-featured non-TUI fallback using Rich.

Works in dumb terminals; same services/policy/runtime as the TUI.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .agents.runtime import AgentCancelled
from .config import BackendName, PermissionMode
from .core import Services
from .doctor import run_checks, summarize
from .sessions import make_title


class PlainApp:
    def __init__(self, svc: Services, resume_sid: str | None = None) -> None:
        self.svc = svc
        self._cancel: asyncio.Event | None = None
        self.console = Console()
        self.rt = svc.runtime()
        self.tok_s = 0.0
        if resume_sid:
            sess = svc.sessions.get(resume_sid)
            if sess is None:
                self.console.print(f"[red]no session {resume_sid}[/red]")
            sess = sess or svc.new_session()
        else:
            sess = svc.new_session()
        self.sess = sess

    # ------------------------------------------------------------------ run
    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        try:
            def _sigint() -> None:
                if self._cancel is not None:
                    self._cancel.set()

            loop.add_signal_handler(signal.SIGINT, _sigint)
        except (NotImplementedError, AttributeError):
            pass  # Windows: KeyboardInterrupt path handles it
        await self._backend_banner()
        self._print_help()
        while True:
            try:
                line = await asyncio.to_thread(input, "\nyou ❯ ")
            except (EOFError, KeyboardInterrupt):
                self.console.print("\nbye — chats & memory are saved.")
                return 0
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if await self._slash(line):
                    return 0
                continue
            await self._turn(line)

    async def _backend_banner(self) -> None:
        be = self.svc.backend()
        ok, info = await be.health()
        if ok:
            self.console.print(f"[green]●[/green] {be.display_name} live  ({self.svc.current_model() or 'no model yet'})  mode={self.svc.config.permission_mode.value}")
            return
        res = await be.ensure_running()
        if res.ok:
            self.console.print(f"[green]●[/green] {be.display_name}: {res.message}")
        else:
            self.console.print(Panel("\n".join([res.message or "server not running", *(list(res.instructions))]), title=f"{be.display_name} — manual start needed", border_style="yellow"))
            self.console.print("[dim]Type /backend mock to keep exploring without a server, or fix the above and /backend again.[/dim]")

    # --------------------------------------------------------------- streaming
    async def _turn(self, user_text: str, *, memory_directive: bool = False) -> None:
        buf = Text()
        live = None
        if self.svc.config.generation.streaming:
            live = Live(buf, console=self.console, refresh_per_second=14, vertical_overflow="visible")
            live.start()

        def on_event(ev: dict[str, Any]) -> None:
            nonlocal buf
            t = ev.get("type")
            if t == "token":
                buf.append(ev.get("text", ""))
            elif t == "tool_plan":
                self._render_plan(ev)
            elif t == "tool_result":
                if live:
                    live.stop()
                mark = "✅" if ev.get("ok") else "❌"
                self.console.print(f"{mark} tool {ev.get('name')} — {(ev.get('output') or '')[:400]}")
                if live:
                    live.start()
            elif t == "memory_activity":
                self.console.print(f"🧠 memory: {ev.get('tool')} ({'saved' if ev.get('ok') else 'failed'})")
            elif t == "retry":
                self.console.print(f"[yellow]↻ correcting model output: {ev.get('reason','')[:160]}[/yellow]")
            elif t == "error":
                if live:
                    live.stop()
                self.console.print(f"[red]⚠ {ev.get('message')}[/red]")

        self._cancel = asyncio.Event()
        result = None
        try:
            result = await self.rt.run_turn(
                self.sess, user_text,
                prompt_fn=self._prompt, on_event=on_event,
                cancel=self._cancel, user_requested_memory=memory_directive,
            )
        except AgentCancelled:
            self.console.print("[yellow]⚠ turn cancelled[/yellow]")
        except KeyboardInterrupt:
            # Ctrl+C unwinds the await — partial messages still belong on disk
            self.console.print("[yellow]⚠ turn interrupted[/yellow]")
        finally:
            if live:
                live.stop()
            self.svc.sessions.save(self.sess)  # every path: crash/Ctrl+C included
        if result is None:
            return
        self.tok_s = result.tokens_per_s or self.tok_s
        if not result.text and result.error is None:
            self.console.print("[dim](no text produced)[/dim]")
        if result.error:
            self.console.print(f"[red]backend error: {result.error}[/red]  (try /backend)")

    def _render_plan(self, ev: dict[str, Any]) -> None:
        head = f"⚙ {ev['name']} [{ev['level']}, mode={ev['mode']}]"
        self.console.print(Text(head, style="cyan"))
        preview = ev.get("preview") or ""
        if preview and (ev.get("approval") or ev.get("allowed") is False):
            self.console.print(Panel(preview[:1500], title="preview", border_style="dim"))

    async def _prompt(self, req: Any) -> Any:
        from .safety import ApprovalOutcome

        d = req.decision
        self.console.print(Panel(
            f"tool: [bold]{req.tool}[/bold]   risk: {d.level} ({d.risk})   mode: {self.svc.config.permission_mode.value}\n"
            + (req.preview or "")[:1800] + "\n" + d.summary(),
            title="approval required", border_style="yellow",
        ))
        ans = (await asyncio.to_thread(input, "approve? [y]es / [n]o / [a]lways this session / [p]ermanent rule: ")).strip().lower()
        if ans in ("p", "permanent"):
            return ApprovalOutcome.APPROVED_ALWAYS
        if ans in ("a", "always", "session"):
            return ApprovalOutcome.APPROVED_SESSION
        if ans.startswith("y"):
            return ApprovalOutcome.APPROVED
        return ApprovalOutcome.DENIED

    # ---------------------------------------------------------------- slash
    async def _slash(self, line: str) -> bool:
        """True = quit."""
        parts = line[1:].split(maxsplit=1)
        cmd = (parts[0] or "").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        c = self.console
        if cmd in ("quit", "exit", "q"):
            c.print("bye — chats & memory are saved.")
            return True
        if cmd == "help":
            self._print_help()
        elif cmd == "model":
            await self._models(arg)
        elif cmd == "backend":
            await self._backend(arg)
        elif cmd == "mode":
            self._mode(arg)
        elif cmd in ("memory", "soul"):
            await self._memory(arg)
        elif cmd == "doctor":
            self._doctor()
        elif cmd == "clear":
            self.sess = self.svc.new_session()
            c.print("[dim]new chat started.[/dim]")
        elif cmd == "new":
            self.sess = self.svc.new_session()
            c.print(f"[dim]chat {self.sess.id} started.[/dim]")
        elif cmd == "chats":
            self._chats(arg)
        elif cmd == "rename":
            s = self.svc.sessions.rename(self.sess.id, arg or make_title(" ".join(
                m.get("content", "") for m in self.sess.messages if m.get("role") == "user")[:80]))
            if s:
                self.sess = s
                c.print(f"renamed → {s.title}")
        elif cmd == "delete":
            self._delete(arg)
        elif cmd == "export":
            self._export(arg)
        elif cmd == "settings":
            # NEVER dump raw config: [cloud] keys would land in terminal
            # scrollback / logs / bug reports. Same masking as the bridge.
            from .bridge import _safe_config
            c.print_json(json.dumps(_safe_config(self.svc.config), indent=2))
        elif cmd == "remember":
            await self._turn(f"remember that {arg}", memory_directive=True)
        else:
            c.print(f"[red]unknown command /{cmd}[/red] — try /help")
        return False

    async def _models(self, arg: str) -> None:
        from .vram import human_bytes

        be_name = arg.split()[0] if arg and " " in arg else None
        key = be_name or self.svc.config.default_backend.value
        be = self.svc.backend(key)
        ok, _ = await be.health()
        if not ok:
            res = await be.ensure_running()
            if not res.ok:
                self.console.print(f"[yellow]{key}: {res.message}[/yellow]")
                self.console.print("[dim]models while offline: set /backend mock to test the picker.[/dim]")
                return
        try:
            models = await be.list_models()
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"[red]{exc}[/red]")
            return
        wanted = arg if not be_name else " ".join(arg.split()[1:])
        if wanted and wanted in {m.name for m in models}:
            self._set_default(key, wanted)
            return
        for m in models:
            star = "★" if m.name == self.svc.current_model(key) else " "
            cap = {True: "tools:native", False: "tools:text", None: "tools:?"}[m.tool_capable]
            size = human_bytes(m.size_bytes) if m.size_bytes else "?"
            self.console.print(f"{star} [bold]{m.name}[/bold]  {size}  {m.quant or '?'}  {cap}  {m.why_tool_weak()[:60]}")
        self.console.print("[dim]choose with: /model <name>[/dim]")

    def _set_default(self, backend_key: str, model: str) -> None:
        cfg = self.svc.config
        section = getattr(cfg, backend_key, None)
        if section is not None and hasattr(section, "default_model"):
            section.default_model = model
            if cfg.default_backend.value != backend_key:
                cfg.default_backend = BackendName(backend_key)
            self.svc.save_config()
            self.console.print(f"[green]default model → {backend_key}:{model}[/green]")

    async def _backend(self, arg: str) -> None:
        if arg in {b.value for b in BackendName}:
            self.svc.config.default_backend = BackendName(arg)
            self.svc.save_config()
            self.rt = self.svc.runtime()
            self.console.print(f"[green]backend → {arg}[/green]")
        await self._backend_banner()
        models = []
        try:
            models = await self.svc.backend().list_models()
        except Exception:  # noqa: BLE001
            pass
        if models:
            self.console.print(f"[dim]{len(models)} model(s) available; /model to pick[/dim]")
        elif self.svc.config.default_backend.value != "mock":
            self.console.print("[yellow]no models — pull one via /model or the TUI, or try /backend mock[/yellow]")

    def _mode(self, arg: str) -> None:
        if not arg:
            for m in PermissionMode:
                marker = "▶" if m is self.svc.config.permission_mode else " "
                self.console.print(f"{marker} {m.value:<12} {m.description[:70]}")
            return
        try:
            mode = PermissionMode(arg.lower())
        except ValueError:
            self.console.print(f"[red]unknown mode '{arg}'[/red]")
            return
        phrase = None
        if mode is PermissionMode.FULL_ACCESS:
            self.console.print(f"[bold red]FULL ACCESS: no prompts. Type the phrase {self.svc.full_access_phrase()!r} to confirm.[/bold red]")
            phrase = (self.console.input("phrase> ")).strip()
        ok = self.svc.set_mode(mode, phrase=phrase)
        self.console.print("[green]mode set[/green]" if ok else "[red]phrase mismatch — mode unchanged[/red]")

    async def _memory(self, arg: str) -> None:
        if arg == "edit":
            import os

            editor = os.environ.get("EDITOR", "vi")
            code = await asyncio.to_thread(os.system, f'{editor} "{self.svc.soul.path}"')
            if code == 0:
                self.console.print("[green]soul.md saved[/green]")
            return
        if arg.startswith("forget "):
            await self._turn(f"please forget {arg[7:]}", memory_directive=True)
            return
        text = self.svc.soul.read()
        self.console.print(Panel(Markdown(text), title=f"soul.md — {self.svc.soul.path}", border_style="magenta"))
        recent = self.svc.soul.recent(5)
        for r in recent:
            self.console.print(f"[dim]· {r.get('ts','')[:16]} {r.get('action')}: {r.get('text','')[:80]}[/dim]")

    def _doctor(self) -> None:
        checks = run_checks(self.svc.config_store, self.svc.config)
        for chk in checks:
            style = {"pass": "green", "warn": "yellow", "fail": "red", "info": "dim"}[chk.status]
            self.console.print(f"[{style}]{chk.icon} {chk.label:<38}[/] {chk.detail}")
            if chk.fix:
                self.console.print(f"    [dim]fix: {chk.fix}[/dim]")
        self.console.print(summarize(checks))

    def _chats(self, arg: str) -> None:
        from datetime import datetime

        if arg.startswith("open "):
            try:
                idx = int(arg[5:].strip())
            except ValueError:
                self.console.print("[red]usage: /chats open <number>[/red]")
                return
            rows = self.svc.sessions.list()
            if 1 <= idx <= len(rows):
                self.sess = rows[idx - 1]
                self.console.print(f"[green]resumed {self.sess.title} ({self.sess.message_count} msgs)[/green]")
            return
        rows = self.svc.sessions.list()
        if not rows:
            self.console.print("[dim]no chats yet[/dim]")
            return
        for i, s in enumerate(rows[:20], 1):
            when = datetime.fromtimestamp(s.updated_at).strftime("%m-%d %H:%M")
            pin = "📌" if s.pinned else "  "
            self.console.print(f"{i:>3}. {pin}[bold]{s.title[:40]:<40}[/] {when}  {s.model[:18]}  {s.message_count}m")
        self.console.print("[dim]/chats open <n> · /export · /rename · /delete <id>[/dim]")

    def _delete(self, arg: str) -> None:
        sid = arg or self.sess.id
        s = self.svc.sessions.get(sid)
        if not s:
            self.console.print("[red]session not found[/red]")
            return
        confirm = self.console.input(f"delete '[bold]{s.title}[/]'? moved to trash. export first? [y]es export, [d]elete only, [n]o: ").strip().lower()
        if confirm.startswith("n") or not confirm:
            return
        if confirm.startswith("y"):
            self._export(f"{sid} markdown")
        path = self.svc.sessions.delete(sid)
        self.console.print(f"[green]deleted to trash: {path}[/green]")
        if sid == self.sess.id:
            self.sess = self.svc.new_session()

    def _export(self, arg: str) -> None:
        from .sessions.export import export

        sid = self.sess.id
        fmt = "markdown"
        parts = arg.split()
        for p in parts:
            if p in ("json", "markdown", "md"):
                fmt = p
            elif len(p) == 12:
                sid = p
        sess = self.svc.sessions.get(sid) or self.sess
        path = export(self.svc.sessions, sess, fmt)
        self.console.print(f"[green]exported → {path}[/green]")

    def _print_help(self) -> None:
        self.console.print(Panel(
            "/help /model /backend /mode /soul /memory [edit|forget …] /doctor /settings\n"
            "/chats [open N] /new /rename <title> /delete [id] /export [json|markdown] /clear /quit\n"
            "Plain mode uses the SAME permission engine as the TUI — blocked is blocked.",
            title="OhMyLLM plain mode", border_style="cyan",
        ))


def run_plain(workspace: str | None = None, resume: str | None = None) -> int:
    svc = Services.boot(workspace)
    try:
        return asyncio.run(PlainApp(svc, resume).run())
    except KeyboardInterrupt:
        print("\nbye — saved.")
        return 0


if __name__ == "__main__":
    sys.exit(run_plain())
