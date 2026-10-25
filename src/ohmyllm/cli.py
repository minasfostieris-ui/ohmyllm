"""OhMyLLM command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__, paths
from .config import BackendName, Config, PermissionMode
from .core import Services
from .doctor import run_checks, summarize

app = typer.Typer(
    name="ohmyllm",
    help="OhMyLLM — local-first agentic AI platform for Ollama & LM Studio.",
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
)
chats_app = typer.Typer(help="Browse and manage chat sessions.", name="chats")
app.add_typer(chats_app)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ohmyllm {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    plain: bool = typer.Option(False, "--plain", "-p", help="Non-TUI fallback mode (basic terminals)."),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Project/workspace directory."),
    resume: str | None = typer.Option(None, "--resume", help="Resume a chat session id."),
    version: bool = typer.Option(False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version and exit."),
) -> None:
    ctx.obj = {"plain": plain, "workspace": workspace, "resume": resume}
    if ctx.invoked_subcommand is None:
        _launch(plain=plain, workspace=workspace, resume=resume)


def _launch(plain: bool, workspace: str | None, resume: str | None) -> None:
    if plain:
        from .plain import run_plain

        raise typer.Exit(run_plain(workspace, resume))
    try:
        from .tui.app import OhMyLLMApp

        OhMyLLMApp(workspace=workspace, resume_sid=resume).run()
    except Exception as exc:  # noqa: BLE001 — terminal lacking TTY etc.
        text = f"{type(exc).__name__}: {exc}".lower()
        if "terminal" in text or "tty" in text or "not supported" in text or "no input" in text:
            console.print(f"[yellow]TUI unavailable here ({exc}); starting plain mode.[/yellow]")
            from .plain import run_plain

            raise typer.Exit(run_plain(workspace, resume)) from exc
        raise


# ----------------------------------------------------------------- commands
@app.command()
def chat(
    message: str | None = typer.Argument(None, help="Optional first message (one-shot when given)."),
    plain: bool = typer.Option(False, "--plain", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    resume: str | None = typer.Option(None, "--resume"),
) -> None:
    """Start chatting (TUI by default, --plain for fallback)."""
    if message:
        _run_one(message, model=model)
        return
    _launch(plain, None, resume)


@app.command()
def models(
    backend: str | None = typer.Option(None, "--backend", "-b", help="ollama | lmstudio | mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List models with size, quantization and VRAM estimates."""
    svc = Services.boot()
    key = backend or svc.config.default_backend.value
    be = svc.backend(key)
    ok, _ = asyncio.run(_health(be))
    if not ok:
        console.print(f"[yellow]{key}: server not running — starting probe offline list is impossible.[/yellow]")
        console.print("Run [bold]ohmyllm backend --start[/bold] or start it manually.")
        raise typer.Exit(2)
    rows = asyncio.run(be.list_models())

    gpu = _gpu_totals()
    if as_json:
        console.print_json(json.dumps([r.__dict__ for r in rows], default=str))
        return
    t = Table(title=f"{len(rows)} model(s) @ {key}")
    for col in ("default", "model", "size", "quant", "ctx", "VRAM min/comfort", "tools"):
        t.add_column(col)
    cur = svc.current_model(key)
    for r in sorted(rows, key=lambda x: x.name):
        est = r.vram_estimate(svc.config.settings_for(be.name, r.name), gpu[0], gpu[1])
        vram = f"{_gb(est.min_bytes)}/{_gb(est.comfortable_bytes)}" if est else "?"
        cap = {True: "native ✓", False: "text", None: "?"}[r.tool_capable]
        t.add_row("★" if r.name == cur else "", r.name, _bytes(r.size_bytes), r.quant or "?",
                  str(r.ctx_len or "?"), vram, cap)
    console.print(t)
    for r in rows:
        if not r.tool_capable and r.why_tool_weak():
            console.print(f"[dim]{r.name}: {r.why_tool_weak()}[/dim]")


@app.command()
def backend(
    set_to: str | None = typer.Argument(None, help="ollama | lmstudio | mock"),
    start: bool = typer.Option(False, "--start", help="Try to start the server."),
) -> None:
    """Show backend status, switch default backend, or start its server."""
    svc = Services.boot()
    if set_to:
        try:
            svc.config.default_backend = BackendName(set_to.lower())
        except ValueError:
            console.print(f"[red]unknown backend {set_to!r}[/red]")
            raise typer.Exit(1) from None
        svc.save_config()
        console.print(f"[green]default backend → {set_to}[/green]")
    names = [svc.config.default_backend.value] if not set_to else [set_to.lower()]
    for name in names:
        be = svc.backend(name)
        ok, info = asyncio.run(_health(be))
        installed = be.is_installed()
        dot = "[green]●[/green]" if ok else "[red]○[/red]"
        console.print(f"{dot} [bold]{be.display_name}[/bold]  installed={installed} running={ok}  {info}")
        if start and not ok:
            res = asyncio.run(be.ensure_running())
            console.print(("[green]✔[/green] " if res.ok else "[yellow]✖[/yellow] ") + res.message)
            for line in res.instructions:
                console.print(f"    [dim]{line}[/dim]")
            raise typer.Exit(0 if res.ok else 3)


@app.command()
def doctor(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Diagnose the environment: python, paths, backends, GPU, config, memory."""
    svc = Services.boot()
    checks = run_checks(svc.config_store, svc.config)
    if as_json:
        console.print_json(json.dumps([c.__dict__ for c in checks], default=str))
    else:
        t = Table(title="OhMyLLM doctor", show_lines=False)
        t.add_column("")
        t.add_column("check")
        t.add_column("detail", overflow="fold")
        t.add_column("fix", overflow="fold", style="dim")
        for c in checks:
            style = {"pass": "green", "warn": "yellow", "fail": "red", "info": "dim"}[c.status]
            t.add_row(f"[{style}]{c.icon}[/]", c.label, c.detail, c.fix)
        console.print(t)
        console.print(summarize(checks))
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(1)


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config and soul."),
    backend_name: str | None = typer.Option(None, "--backend", "-b"),
) -> None:
    """Create config + soul.md + directories (idempotent)."""
    paths.ensure_global_dirs()
    paths.ensure_project_dirs()
    store = Services.boot().config_store
    existed = store.exists()
    if existed and not force:
        console.print(f"[green]config already present[/green] at {store.path} (use --force to reset)")
    else:
        cfg = Config()
        if backend_name:
            cfg.default_backend = BackendName(backend_name.lower())
        store.save(cfg)
        console.print(f"[green]wrote config[/green] → {store.path}")
    from .memory import SoulManager

    soul = SoulManager(paths.soul_path(), paths.soul_backups_dir())
    if force and soul.exists():
        soul.clear()
        console.print("[green]soul.md reset[/green]")
    else:
        soul.ensure()
        console.print(f"[green]soul.md ready[/green] → {soul.path}")
    console.print(Panel(
        f"Next: run [bold]ohmyllm[/bold] to open the TUI wizard, or [bold]ohmyllm run 'hello'[/bold] for a one-shot.\n"
        f"Docs: {Path(__file__).parent.parent.parent / 'README.md'}",
        title="initialized", border_style="green",
    ))


@app.command()
def soul(
    show: bool = typer.Option(True, "--show/--no-show"),
    edit: bool = typer.Option(False, "--edit", help="Open in $EDITOR."),
    path_only: bool = typer.Option(False, "--path"),
) -> None:
    """Show, open, or locate soul.md."""
    from .memory import SoulManager

    mgr = SoulManager(paths.soul_path(), paths.soul_backups_dir())
    mgr.ensure()
    if path_only:
        print(mgr.path)
        return
    if edit:
        editor = os.environ.get("EDITOR", "vi")
        code = os.system(f'{editor} "{mgr.path}"')
        raise typer.Exit(code)
    console.print(Panel(mgr.read(), title=f"soul.md — {mgr.path}", border_style="magenta"))


@app.command()
def run(
    task: str = typer.Argument(..., help="One-shot agentic task."),
    backend_name: str | None = typer.Option(None, "--backend", "-b"),
    model: str | None = typer.Option(None, "--model", "-m"),
    mode: str | None = typer.Option(None, "--mode", help="Permission mode for this run."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve tool calls this run."),
) -> None:
    """Run a single agentic task from the shell (permission mode enforced)."""
    _run_one(task, model=model, backend_name=backend_name, mode=mode, yes=yes)


def _run_one(task: str, model: str | None = None, backend_name: str | None = None,
             mode: str | None = None, yes: bool = False) -> None:
    svc = Services.boot()
    if backend_name:
        svc.config.default_backend = BackendName(backend_name)
    if mode:
        try:
            m = PermissionMode(mode)
        except ValueError:
            console.print(f"[red]unknown mode {mode!r}[/red]")
            raise typer.Exit(1) from None
        if m is PermissionMode.FULL_ACCESS:
            console.print("[red]full_access cannot be granted from the CLI; use the TUI phrase confirmation.[/red]")
            raise typer.Exit(1)
        svc.config.permission_mode = m
    be = svc.backend()
    ok, _ = asyncio.run(_health(be))
    if not ok:
        res = asyncio.run(be.ensure_running())
        if not res.ok:
            console.print(f"[red]{res.message}[/red]")
            for line in res.instructions:
                console.print(f"  [dim]{line}[/dim]")
            raise typer.Exit(3)
    from .sessions import make_title

    sess = svc.sessions.create(
        title=make_title(task), first_message="", backend=be.name.value,
        model=model or svc.current_model(), permission_mode=svc.config.permission_mode.value,
        workspace=str(svc.workspace),
    )
    rt = svc.runtime()
    from rich.live import Live
    from rich.text import Text

    buf = Text()

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "token":
            buf.append(ev.get("text", ""))
        elif t == "tool_plan":
            console.print(f"[cyan]⚙ {ev['name']} ({ev['level']}, approval={'yes' if ev['approval'] else 'no'})[/cyan]")
        elif t == "tool_result":
            mark = "✅" if ev.get("ok") else "❌"
            console.print(f"{mark} {ev.get('name')}: {(ev.get('output') or '')[:200]}")
        elif t == "retry":
            console.print(f"[yellow]↻ {ev.get('reason','')[:150]}[/yellow]")
        elif t == "error":
            console.print(f"[red]{ev.get('message')}[/red]")

    from .safety import ApprovalOutcome

    async def prompt_fn(req) -> ApprovalOutcome:
        if yes:
            return ApprovalOutcome.APPROVED_SESSION
        label = f"allow [bold]{req.tool}[/bold]? {req.preview[:200]}"
        ans = await asyncio.to_thread(console.input, label + " [y/N] ")
        return ApprovalOutcome.APPROVED if ans.strip().lower() in ("y", "yes") else ApprovalOutcome.DENIED

    try:
        with Live(buf, console=console, transient=True):
            result = asyncio.run(rt.run_turn(
                sess, task, model=model, on_event=on_event, prompt_fn=prompt_fn,
            ))
    finally:
        svc.sessions.save(sess)
    bits = [sess.model or ""]
    tok = (result.usage or {}).get("eval_count")
    if tok:
        bits.append(f"{tok} tok")
    if result.ttft_s:
        bits.append(f"ttft {result.ttft_s:.1f}s")
    if result.tokens_per_s:
        bits.append(f"{result.tokens_per_s:.1f} t/s")
    if result.steps:
        bits.append(f"{result.steps} step{'s' if result.steps > 1 else ''}")
    if result.calls:
        bits.append(f"{len(result.calls)} tool call{'s' if len(result.calls) > 1 else ''}")
    console.print(f"\n[bold]result:[/bold] {result.text}")
    console.print("[dim]· " + " · ".join(b for b in bits if b) + "[/dim]")
    if result.error:
        raise typer.Exit(4)


@app.command()
def logs(n: int = typer.Option(30, "-n", "--lines")) -> None:
    """Show recent application log lines."""
    p = paths.app_log_path()
    if not p.is_file():
        console.print("[dim]no log file yet[/dim]")
        return
    lines = p.read_text(errors="replace").splitlines()
    for ln in lines[-n:]:
        console.print(ln)


@app.command()
def audit(n: int = typer.Option(30, "-n", "--lines")) -> None:
    """Show recent audit events."""
    from .safety import AuditLogger, format_event

    rows = AuditLogger().recent(n)
    if not rows:
        console.print("[dim]no audit events yet[/dim]")
        return
    for r in rows:
        console.print(format_event(r))


@app.command(name="self-install")
def self_install(
    write_profile: bool = typer.Option(False, "--write-profile", help="Append PATH lines to shell profile (opt-in)."),
) -> None:
    """Install global command aliases (ohmyllm/omllm/oml/OMLLM/OML) + PATH guidance."""
    from .selfinstall import (
        append_to_profile,
        ensure_aliases,
        path_dirs_missing_from_env,
        path_instructions,
    )

    results = ensure_aliases()
    t = Table(title="global commands")
    t.add_column("name")
    t.add_column("ok")
    t.add_column("location")
    t.add_column("note")
    for r in results:
        t.add_row(r.name, "[green]✔[/green]" if r.ok else "[red]✖[/red]", r.location, r.note)
    console.print(t)
    missing = path_dirs_missing_from_env()
    if missing:
        if write_profile:
            for f in append_to_profile(missing):
                console.print(f"[green]updated[/green] {f}")
            console.print("Reload your shell (or open a new terminal) to pick it up.")
        console.print("[bold]If the commands aren't found, add to PATH:[/bold]")
        for shell, lines in path_instructions(missing).items():
            console.print(f"  [cyan]{shell}[/cyan]")
            for ln in lines:
                console.print(f"    [dim]{ln}[/dim]")


# ------------------------------------------------------------------- chats
@chats_app.command("list")
def chats_list(n: int = typer.Option(30, "-n")) -> None:
    """List stored chat sessions."""
    svc = Services.boot()
    rows = svc.sessions.list()[:n]
    if not rows:
        console.print("[dim]no chats yet — start one with `ohmyllm`[/dim]")
        return
    t = Table(title=f"chat sessions ({len(svc.sessions.list())})")
    for col in ("#", "id", "updated", "title", "model", "msgs", "pin", "workspace"):
        t.add_column(col)
    for i, s in enumerate(rows, 1):
        t.add_row(str(i), s.id, datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M"),
                  s.title[:44], f"{s.backend}:{s.model}"[:28], str(s.message_count),
                  "📌" if s.pinned else "", s.workspace[:28])
    console.print(t)


@chats_app.command("open")
def chats_open(session_id: str, plain: bool = typer.Option(False, "--plain", "-p")) -> None:
    """Resume a chat (TUI or --plain)."""
    svc = Services.boot()
    sess = svc.sessions.resolve(session_id)
    if sess is None:
        console.print(f"[red]no session {session_id!r}[/red] — see `ohmyllm chats list`")
        raise typer.Exit(1)
    _launch(plain, None, sess.id)


@chats_app.command("delete")
def chats_delete(
    session_id: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    export_first: bool = typer.Option(True, "--export/--no-export", help="Export markdown before deleting."),
) -> None:
    """Delete a chat (goes to trash, never hard-deleted)."""
    from .sessions.export import export

    svc = Services.boot()
    sess = svc.sessions.resolve(session_id)
    if sess is None:
        console.print(f"[red]no session {session_id!r}[/red]")
        raise typer.Exit(1)
    if not yes:
        typer.confirm(f"delete '{sess.title}' ({sess.message_count} msgs)?", abort=True)
    if export_first:
        p = export(svc.sessions, sess, "markdown")
        console.print(f"[dim]exported first → {p}[/dim]")
    trash = svc.sessions.delete(sess.id)
    console.print(f"[green]moved to trash:[/green] {trash}")


@chats_app.command("export")
def chats_export(
    session_id: str,
    fmt: str = typer.Option("markdown", "--format", "-f", help="markdown | json"),
    out: Path | None = typer.Option(None, "--out", "-o"),
) -> None:
    """Export a chat to markdown or JSON."""
    from .sessions.export import export

    svc = Services.boot()
    sess = svc.sessions.resolve(session_id)
    if sess is None:
        console.print(f"[red]no session {session_id!r}[/red]")
        raise typer.Exit(1)
    p = export(svc.sessions, sess, fmt, dest=out)
    console.print(f"[green]exported → {p}[/green]")


@app.command("skills")
def skills_cmd(
    import_path: str | None = typer.Argument(None, help="Folder containing SKILL.md (or a parent of skill folders) to import."),
    extra: str | None = typer.Argument(None, help="Second token when you write `skills import <path>`."),
) -> None:
    """List imported skill guides, or import them from a folder."""
    if import_path and import_path.strip().lower() == "import" and extra:
        import_path = extra
    from . import paths
    from .skills import SkillManager

    mgr = SkillManager(paths.skills_dir())
    if import_path:
        try:
            names = mgr.import_path(import_path)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
        console.print(f"[green]imported {len(names)} skill(s)[/green] → {paths.skills_dir()}")
        for n in names[:40]:
            console.print(f"  · {n}")
        return
    rows = mgr.list()
    if not rows:
        console.print(f"[dim]no skills yet — `oml skills import <folder>` (skills live in {paths.skills_dir()})[/dim]")
        return
    for sk in rows:
        console.print(f"[b]{sk.name}[/b] — {sk.description[:100]}")


@app.command()
def gui(
    browser: bool = typer.Option(False, "--browser", "-b", help="Force the web-browser console instead of the native window."),
    stop: bool = typer.Option(False, "--stop", help="Close the background web console."),
    no_open: bool = typer.Option(False, "--no-open", help="Print the console URL but do not launch anything."),
    port: int = typer.Option(0, "--port", help="Pin the loopback bridge port (0 = random)."),
    workspace: str = typer.Option(None, "--workspace", "-w", help="Workspace root for this session."),
) -> None:
    """Open the OhMyLLM console: native Tauri window when available, browser otherwise.

    Both modes run the exact same engine — the loopback bridge reuses Services,
    the policy engine and the approval flow, bound to 127.0.0.1 with a
    per-process token.
    """
    import shutil
    import subprocess
    import tempfile
    import time
    import webbrowser

    if stop:
        stopf = Path.home() / ".ohmyllm" / "gui-web.pid"
        if stopf.is_file():
            try:
                os.kill(int(stopf.read_text().strip()), 15)
                console.print("[green]web console closed[/green]")
            except (ValueError, OSError):
                console.print("[dim]web console was not running[/dim]")
            stopf.unlink(missing_ok=True)
        else:
            console.print("[dim]no web console running[/dim]")
        return

    env = dict(os.environ)
    env["OMLLM_PYTHON"] = sys.executable
    if workspace:
        env["OMLLM_WORKSPACE"] = str(Path(workspace).expanduser().resolve())

    native = None
    if not browser:
        cand = []
        if v := env.get("OMLLM_GUI_BIN"):
            cand.append(Path(v))
        here = Path(__file__).resolve()
        for base in here.parents:
            cand.append(base / "gui" / "src-tauri" / "target" / "release" / "ohmyllm-gui")
            if (base / ".git").exists():
                break
        cand.append(Path.home() / ".local" / "bin" / "ohmyllm-gui")
        if w := shutil.which("ohmyllm-gui"):
            cand.insert(0, Path(w))
        native = next((c for c in cand if c.is_file()), None)

    if native is not None:
        # app boots in the background; the CLI stays free and prints the
        # optional localhost web console
        subprocess.Popen([str(native)], env=env, start_new_session=True)
        console.print(f"[green]native console launching in the background[/green] [dim]({native})[/dim]")
        stopf = Path.home() / ".ohmyllm" / "gui-web.pid"
        if stopf.is_file():
            try:
                import os as _os
                _os.kill(int(stopf.read_text().strip()), 0)
                console.print("[dim]web console already running — `oml gui --stop` closes it[/dim]")
                return
            except (ValueError, OSError):
                pass
        wport = Path(tempfile.gettempdir()) / f"ohmyllm-gui-web-{os.getpid()}.port"
        wport.unlink(missing_ok=True)
        wargs = [sys.executable, "-m", "ohmyllm.bridge", "--port-file", str(wport)]
        wchild = subprocess.Popen(wargs, env=env, start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stopf.parent.mkdir(parents=True, exist_ok=True)
        stopf.write_text(str(wchild.pid))
        deadline0 = time.time() + 8
        while time.time() < deadline0:
            if wport.is_file() and len(wport.read_text().strip().split(" ")) == 2:
                a, t = wport.read_text().strip().split(" ")
                console.print(f"[bold]web console (optional)[/bold] → [link]http://127.0.0.1:{a}/?t={t}[/link]")
                console.print("[dim]loopback only · token required · `oml gui --stop` closes it[/dim]")
                return
            time.sleep(0.1)
        console.print("[dim]web console did not come up in 8s — the native window still works[/dim]")
        return

    if not browser:
        console.print("[yellow]Native window not built yet — falling back to the browser console.[/yellow]")
        console.print("[dim]To get the native app: install Tauri prerequisites, then "
                      "run `cargo build --release` inside `gui/src-tauri` (or set OMLLM_GUI_BIN).[/dim]")

    port_file = Path(tempfile.gettempdir()) / f"ohmyllm-gui-{os.getpid()}.port"
    port_file.unlink(missing_ok=True)
    args = [sys.executable, "-m", "ohmyllm.bridge"]
    if port:
        args += ["--port", str(port)]
    args += ["--port-file", str(port_file)]
    child = subprocess.Popen(args, env=env)
    try:
        deadline = time.time() + 15
        info = ""
        while time.time() < deadline:
            if port_file.exists():
                info = port_file.read_text().strip()
                if len(info.split(" ")) >= 2:
                    break
            if child.poll() is not None:
                console.print("[red]bridge failed to start — run `ohmyllm doctor` for details.[/red]")
                raise typer.Exit(1)
            time.sleep(0.05)
        else:
            console.print("[red]bridge did not come up within 15s[/red]")
            child.kill()
            raise typer.Exit(1)
        actual, token = info.split(" ", 1)
        url = f"http://127.0.0.1:{actual}/?t={token}"
        console.print(Panel(f"[bold green]OhMyLLM console[/bold green] → [link]{url}[/link]\n"
                            f"[dim]loopback only · token required · Ctrl+C closes the session[/dim]",
                            border_style="dim"))
        if not no_open:
            webbrowser.open(url)
        while child.poll() is None:
            time.sleep(0.3)
    except KeyboardInterrupt:
        console.print("[dim]closing console…[/dim]")
    finally:
        if child.poll() is None:
            child.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=4)
        port_file.unlink(missing_ok=True)


# ------------------------------------------------------------------ helpers
async def _health(be) -> tuple[bool, dict]:
    return await be.health()


def _gpu_totals() -> tuple[int | None, int | None]:
    from .vram import detect_gpus

    g = detect_gpus()
    if not g:
        return None, None
    totals = sum(x.total_bytes for x in g)
    frees = [x.free_bytes for x in g if x.free_bytes is not None]
    return totals, (sum(frees) if frees else None)


def _gb(n: float | None) -> str:
    return f"{n / 2**30:.1f}G" if n else "?"


def _bytes(n: int | None) -> str:
    from .vram.detect import human_bytes

    return human_bytes(n) if n else "?"


def app_entry() -> None:  # console_scripts target
    app()


if __name__ == "__main__":
    app_entry()
