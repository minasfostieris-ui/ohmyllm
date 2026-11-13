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
from .secureio import (
    atomic_write,
    editor_argv,
    private_temp_dir,
    read_bridge_discovery,
    read_tail,
    sanitized_environment,
    trusted_executable,
)

app = typer.Typer(
    name="ohmyllm",
    help="OhMyLLM — local-first agentic AI platform for Ollama & LM Studio.",
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
)
chats_app = typer.Typer(help="Browse and manage chat sessions.", name="chats")
app.add_typer(chats_app)
mail_app = typer.Typer(help="Connect an IMAP/SMTP account to the mail tools.", name="mail",
                       invoke_without_command=True)


@mail_app.callback()
def mail_main(ctx: typer.Context) -> None:
    """`oml mail` — shows where you stand and starts setup if mail isn't connected.
    Nothing to memorize: type `oml mail` and do what it says."""
    if ctx.invoked_subcommand is not None:
        return
    from . import mail as mail_mod
    from .config import ConfigStore

    cfg = ConfigStore(paths.config_path()).load()
    if mail_mod.configured(cfg) and cfg.mail.enabled:
        console.print(f"[green]✔ email is connected[/green] — {cfg.mail.address}")
        console.print("[dim]the agent can search, read, draft and send mail from any chat.[/dim]")
        console.print("[dim]change account: oml mail setup · details: oml mail status[/dim]")
        return
    console.print(Panel(
        "[bold]email is not connected yet[/bold] — let's fix that now, about a minute:\n\n"
        "  [bold cyan]1[/bold cyan]  type your email address\n"
        "  [bold cyan]2[/bold cyan]  a browser page opens → sign in → click [b]Generate[/b] → copy the 16-character code\n"
        "  [bold cyan]3[/bold cyan]  paste it back here\n\n"
        "[dim]Ctrl+C at any moment backs out. Drafts already work without an account.[/dim]",
        title="oml mail", border_style="cyan",
    ))
    # explicit None: calling the command function directly would otherwise
    # pass typer's OptionInfo descriptors as the defaults
    try:
        mail_setup(address=None, password=None, imap=None, smtp=None)  # type: ignore[arg-type]
    except (EOFError, KeyboardInterrupt, typer.exceptions.Abort):
        # piped/closed stdin or the user backing out: no traceback
        console.print("[dim]not connected — run [b]oml mail[/b] again when ready[/dim]")
        raise typer.Exit(0) from None
app.add_typer(mail_app)
console = Console()
errors = Console(stderr=True, markup=False, highlight=False)


def _options(ctx: typer.Context, workspace: str | None, resume: str | None = None):
    root = ctx.find_root().obj or {}
    return (workspace if workspace is not None else root.get("workspace"),
            resume if resume is not None else root.get("resume"))


def _failure(message: str, code: int, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message, "exit_code": code}))
    errors.print(message)
    raise typer.Exit(code)


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
def tui(
    ctx: typer.Context,
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Project/workspace directory."),
    resume: str | None = typer.Option(None, "--resume", help="Resume a chat session id."),
) -> None:
    """Open the full TUI (same as plain `oml`)."""
    workspace, resume = _options(ctx, workspace, resume)
    _launch(plain=False, workspace=workspace, resume=resume)


@app.command()
def cli(
    ctx: typer.Context,
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Project/workspace directory."),
    resume: str | None = typer.Option(None, "--resume", help="Resume a chat session id."),
) -> None:
    """Start the plain line-CLI session (basic terminals, pipes, low TTYS)."""
    workspace, resume = _options(ctx, workspace, resume)
    _launch(plain=True, workspace=workspace, resume=resume)


@app.command()
def update(
    check: bool = typer.Option(False, "--check", help="Report the newest version without installing."),
) -> None:
    """Pull the newest OhMyLLM from GitHub and reinstall it in place."""
    import shutil
    import subprocess

    src = "git+https://github.com/minasfostieris-ui/ohmyllm.git"
    if check:
        try:
            out = subprocess.run(["git", "ls-remote", src, "HEAD"], capture_output=True, text=True, timeout=30)
            sha = out.stdout.split()[0][:9] if out.returncode == 0 and out.stdout else "?"
            console.print(f"[bold]ohmyllm {__version__}[/bold] · GitHub HEAD: {sha}")
            console.print("[dim]run `oml update` to install it[/dim]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]could not reach GitHub: {exc}[/red]")
            raise typer.Exit(1) from exc
        return
    if shutil.which("uv"):
        cmd = ["uv", "tool", "install", "--force", src]
    elif shutil.which("pip"):
        cmd = [sys.executable or "pip", "install", "--upgrade", "--force-reinstall", src]
    else:
        console.print("[red]neither uv nor pip found — install one and retry[/red]")
        raise typer.Exit(1)
    console.print(f"[dim]updating from GitHub: {' '.join(cmd)}[/dim]")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        console.print("[green]✔ updated.[/green] Next launch runs the new version — run `oml --version` to confirm.")
    else:
        err = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        console.print("[red]update failed:[/red]\n" + "\n".join(err))
        raise typer.Exit(1)


@app.command()
def chat(
    ctx: typer.Context,
    message: str | None = typer.Argument(None, help="Optional first message (one-shot when given)."),
    plain: bool = typer.Option(False, "--plain", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    resume: str | None = typer.Option(None, "--resume"),
) -> None:
    """Start chatting (TUI by default, --plain for fallback)."""
    workspace, resume = _options(ctx, None, resume)
    plain = plain or bool((ctx.obj or {}).get("plain"))
    if message:
        _run_one(message, model=model, workspace=workspace, resume=resume, plain=plain)
        return
    _launch(plain, workspace, resume)


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
        console.print(f"[yellow]{key}: server is not running — the model list needs a live server.[/yellow]")
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
        import subprocess

        try:
            completed = subprocess.run(
                editor_argv(mgr.path),
                cwd=mgr.path.parent,
                env=sanitized_environment(),
                check=False,
            )
        except (OSError, ValueError) as exc:
            console.print(f"[red]could not launch editor: {exc}[/red]")
            raise typer.Exit(1) from None
        raise typer.Exit(completed.returncode)
    console.print(Panel(mgr.read(), title=f"soul.md — {mgr.path}", border_style="magenta"))


@app.command()
def run(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="One-shot task; '-' reads stdin explicitly."),
    workspace: str | None = typer.Option(None, "--workspace", "-w"),
    resume: str | None = typer.Option(None, "--resume"),
    as_json: bool = typer.Option(False, "--json", help="One JSON result on stdout; diagnostics on stderr."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the final answer."),
    plain: bool = typer.Option(False, "--plain", help="Disable live rendering and decorations."),
    backend_name: str | None = typer.Option(None, "--backend", "-b"),
    model: str | None = typer.Option(None, "--model", "-m"),
    mode: str | None = typer.Option(None, "--mode", help="Permission mode for this run."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve non-shell tool calls this run; shell commands still ask."),
) -> None:
    """Run a single agentic task from the shell (permission mode enforced)."""
    workspace, resume = _options(ctx, workspace, resume)
    if task == "-":
        if sys.stdin.isatty():
            _failure("Pipe task text to stdin when using '-'.", 1, as_json)
        task = sys.stdin.read(65_537)
    if not task.strip() or len(task) > 65_536:
        _failure("Task must contain 1–65536 characters.", 1, as_json)
    _run_one(task, model=model, backend_name=backend_name, mode=mode, yes=yes,
             workspace=workspace, resume=resume, as_json=as_json, quiet=quiet,
             plain=plain or bool((ctx.obj or {}).get("plain")))


def _run_one(task: str, model: str | None = None, backend_name: str | None = None,
             mode: str | None = None, yes: bool = False, *, workspace: str | None = None,
             resume: str | None = None, as_json: bool = False, quiet: bool = False,
             plain: bool = False) -> None:
    svc = Services.boot(workspace=workspace)
    sess = svc.sessions.resolve(resume) if resume else None
    if resume and sess is None:
        _failure(f"No unique session matches {resume!r}; see 'oml chats list'.", 1, as_json)
    if sess and not backend_name:
        backend_name = sess.backend
    try:
        if backend_name:
            svc.config.default_backend = BackendName(backend_name)
    except ValueError:
        _failure(f"Unknown backend {backend_name!r}.", 1, as_json)
    if mode:
        try:
            m = PermissionMode(mode)
        except ValueError:
            _failure(f"Unknown mode {mode!r}.", 1, as_json)
        if m is PermissionMode.FULL_ACCESS:
            _failure("full_access requires interactive phrase confirmation.", 1, as_json)
        # Route through set_mode so the policy_generation bump, runtime
        # retirement and grant invalidation happen exactly like /mode does.
        svc.set_mode(m)
    try:
        be = svc.backend()
        ok, _ = asyncio.run(_health(be))
        if not ok and svc.may_autostart():
            ok = asyncio.run(be.ensure_running()).ok
    except Exception as exc:
        _failure(f"Backend unavailable: {exc}. Run 'oml doctor'.", 3, as_json)
    if not ok:
        _failure("Backend unavailable. Start it explicitly with 'oml backend --start'.", 3, as_json)
    from .sessions import make_title

    sess = sess or svc.sessions.create(
        title=make_title(task), first_message="", backend=be.name.value,
        model=model or svc.current_model(), permission_mode=svc.config.permission_mode.value,
        workspace=str(svc.workspace),
    )
    rt = svc.runtime()
    from rich.live import Live
    from rich.text import Text

    buf = Text()

    def on_event(ev: dict) -> None:
        if as_json or quiet or plain:
            return
        t = ev.get("type")
        if t == "token":
            buf.append(ev.get("text", ""))
        elif t == "tool_plan":
            console.print(f"[cyan]⚙ {ev['name']} ({ev['level']}, approval={'yes' if ev['approval'] else 'no'})[/cyan]")
        elif t == "tool_result":
            mark = "✅" if ev.get("ok") else "❌"
            errors.print(f"{mark} {ev.get('name')}: {(ev.get('output') or '')[:200]}")
        elif t == "retry":
            console.print(f"[yellow]↻ {ev.get('reason','')[:150]}[/yellow]")
        elif t == "error":
            console.print(f"[red]{ev.get('message')}[/red]")

    from .safety import ApprovalOutcome

    async def prompt_fn(req) -> ApprovalOutcome:
        if yes and req.tool != "run_command":
            return ApprovalOutcome.APPROVED
        if as_json or not sys.stdin.isatty():
            return ApprovalOutcome.DENIED
        label = f"Allow {req.tool}? {req.preview[:200]}"
        ans = await asyncio.to_thread(errors.input, label + " [y/N] ")
        return ApprovalOutcome.APPROVED if ans.strip().lower() in ("y", "yes") else ApprovalOutcome.DENIED

    from .agents.runtime import AgentCancelled, TurnResult

    try:
        display = contextlib.nullcontext() if as_json or quiet or plain else Live(buf, console=console, transient=True)
        with display:
            result = asyncio.run(rt.run_turn(
                sess, task, model=model, on_event=on_event, prompt_fn=prompt_fn,
            ))
    except (KeyboardInterrupt, AgentCancelled):
        result = TurnResult(text="", cancelled=True, error="cancelled")
    except Exception as exc:
        result = TurnResult(text="", error=str(exc))
    try:
        svc.sessions.save(sess)
    except Exception as exc:
        _failure(f"Session save failed: {exc}", 4, as_json)
    code = 130 if result.cancelled else 4 if result.error else 0
    if as_json:
        print(json.dumps({"ok": code == 0, "text": result.text, "error": result.error,
                          "session": sess.id, "model": sess.model, "saved": True,
                          "cancelled": result.cancelled, "usage": result.usage,
                          "steps": result.steps, "exit_code": code}, default=str))
    elif quiet or plain:
        print(result.text)
    else:
        console.print(result.text, markup=False, highlight=False)
        errors.print(f"Saved session {sess.id}")
    if result.error:
        errors.print(result.error)
    if code:
        raise typer.Exit(code)
    if as_json or quiet or plain:
        return

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
    errors.print("· " + " · ".join(b for b in bits if b), markup=False, highlight=False)


@app.command(rich_help_panel="Diagnostics")
def status(
    ctx: typer.Context,
    workspace: str | None = typer.Option(None, "--workspace", "-w"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show local configuration status without connecting to a backend."""
    from .config import ConfigStore

    store = ConfigStore()
    cfg = store.load()
    workspace, _ = _options(ctx, workspace)
    data = {"version": __version__, "backend": cfg.default_backend.value,
            "workspace": str(Path(workspace or cfg.workspace or paths.workspace_root()).expanduser().resolve()),
            "mode": "dangerous" if cfg.permission_mode is PermissionMode.FULL_ACCESS else cfg.permission_mode.value,
            "operation_mode": cfg.agent.op_mode, "problems": store.last_problems}
    if as_json:
        print(json.dumps(data))
    else:
        for key, value in data.items():
            console.print(f"{key}: {value}", markup=False, highlight=False)


@app.command()
def logs(n: int = typer.Option(30, "-n", "--lines")) -> None:
    """Show recent application log lines."""
    p = paths.app_log_path()
    if not p.is_file():
        console.print("[dim]no log file yet[/dim]")
        return
    count = max(0, min(int(n), 1000))
    if count == 0:
        return
    try:
        lines = read_tail(p).splitlines()
    except (OSError, ValueError) as exc:
        console.print(f"[yellow]cannot read log safely: {exc}[/yellow]")
        return
    for ln in lines[-count:]:
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


@chats_app.command("search")
def chats_search(
    term: str = typer.Argument(..., help="Search text (FTS over messages + redacted tool metadata)."),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=50),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Full-text search across saved chats (trash excluded, secrets redacted)."""
    svc = Services.boot()
    hits = svc.sessions.search(term, limit)
    if as_json:
        print(json.dumps({"results": hits}))
        return
    if not hits:
        console.print("[dim]no matches[/dim]")
        return
    t = Table(title=f"search: {term}")
    for col in ("id", "updated", "title", "snippet"):
        t.add_column(col, overflow="fold")
    for h in hits:
        t.add_row(str(h["session_id"]), str(h["updated_iso"]), str(h["title"])[:44], str(h["snippet"]))
    console.print(t)


@app.command()
def state(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show the agent machine state (idle/working/blocked/done/error)."""
    import time as _time

    from .secureio import read_private_json

    path = paths.global_home() / "state.json"
    data: dict = {"state": "idle", "session_id": None, "turn": 0, "backend": "", "model": "",
                  "pending_approval_id": None, "seq": 0, "ts": 0.0}
    try:
        loaded = read_private_json(path)
        if isinstance(loaded, dict):
            data.update({k: loaded[k] for k in data if k in loaded})
    except (OSError, ValueError):
        pass  # missing/unreadable state ⇒ idle defaults
    age = _time.time() - float(data["ts"] or 0.0)
    if data["state"] in ("working", "blocked") and float(data["ts"] or 0.0) > 0 and age > 300:
        errors.print(f"warning: state file is stale ({int(age)}s old) — the agent may have exited")
    if as_json:
        print(json.dumps(data))
    else:
        console.print(f"state: {data['state']} · turn {data['turn']} · session {data['session_id'] or '—'} · "
                      f"backend {data['backend'] or '—'} · model {data['model'] or '—'}",
                      markup=False, highlight=False)
        if data["pending_approval_id"]:
            console.print(f"pending approval: {data['pending_approval_id']}", markup=False, highlight=False)


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
    web: bool = typer.Option(False, "--web", help="Open the loopback console in a web browser (explicit opt-in)."),
    browser: bool = typer.Option(False, "--browser", "-b", help="Deprecated alias for --web."),
    stop: bool = typer.Option(False, "--stop", help="Close the background web console."),
    no_open: bool = typer.Option(False, "--no-open", help="Print the console URL but do not launch anything."),
    port: int = typer.Option(0, "--port", help="Pin the loopback bridge port (0 = random)."),
    workspace: str = typer.Option(None, "--workspace", "-w", help="Workspace root for this session."),
) -> None:
    """Open the OhMyLLM console: the native app by default, never a browser.

    The native Tauri window owns its loopback bridge. Without a native
    binary the bridge still starts in the background and its loopback URL
    is printed; a browser opens only on the explicit `--web` opt-in.
    Both modes run the exact same engine — the loopback bridge reuses
    Services, the policy engine and the approval flow, bound to 127.0.0.1
    with a per-process token.
    """
    import subprocess
    import time
    import webbrowser

    if stop:
        stopf = paths.global_home() / "gui-web.pid"
        if stopf.is_file():
            try:
                os.kill(int(stopf.read_text().strip()), 15)
                console.print("[green]web console closed[/green]")
            except (ValueError, OSError):
                console.print("[dim]web console was not running[/dim]")
            stopf.unlink(missing_ok=True)
            (paths.global_home() / "gui-web.discovery").unlink(missing_ok=True)
        else:
            console.print("[dim]no web console running[/dim]")
        return

    env = _bridge_environment(workspace)
    want_web = web or browser

    native = None if want_web else _resolve_native_binary(env)
    if native is not None:
        # App first: the native shell starts (or attaches to) its own bridge.
        # No second bridge here and never an unsolicited browser window.
        stopf = paths.global_home() / "gui-web.pid"
        discf = paths.global_home() / "gui-web.discovery"
        if stopf.is_file() and discf.is_file():
            # A background console is already serving: hand its private
            # discovery to the shell so it attaches instead of stacking.
            with contextlib.suppress(ValueError, OSError):
                os.kill(int(stopf.read_text().strip()), 0)
                read_bridge_discovery(Path(discf.read_text().strip()))
                env["OMLLM_BRIDGE_DISCOVERY"] = discf.read_text().strip()
        subprocess.Popen([str(native)], env=env, start_new_session=True)
        console.print(f"[green]native console launching in the background[/green] [dim]({native})[/dim]")
        console.print("[dim]prefer a browser? `oml gui --web` · `oml gui --stop` closes a background web console[/dim]")
        return

    stopf = paths.global_home() / "gui-web.pid"
    discf = paths.global_home() / "gui-web.discovery"
    if want_web and not no_open and stopf.is_file() and discf.is_file():
        # Reuse the running background console instead of stacking bridges.
        try:
            os.kill(int(stopf.read_text().strip()), 0)
            actual, token = read_bridge_discovery(Path(discf.read_text().strip()))
            webbrowser.open(f"http://127.0.0.1:{actual}/#token={token}")
            console.print(f"[bold]web console[/bold] opened at http://127.0.0.1:{actual}/ [dim](already running)[/dim]")
            return
        except (ValueError, OSError):
            pass  # stale pid or discovery: start a fresh console below

    launch_dir, port_file = _new_bridge_discovery("web-" if want_web else "console-")
    args = [sys.executable, "-m", "ohmyllm.bridge"]
    if port:
        args += ["--port", str(port)]
    args += ["--port-file", str(port_file)]
    background = not want_web
    child = subprocess.Popen(
        args, env=env, start_new_session=background,
        stdout=subprocess.DEVNULL if background else None,
        stderr=subprocess.DEVNULL if background else None,
    )
    kept = False
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if port_file.exists():
                actual, token = read_bridge_discovery(port_file)
                break
            if child.poll() is not None:
                console.print("[red]bridge failed to start — run `ohmyllm doctor` for details.[/red]")
                raise typer.Exit(1)
            time.sleep(0.05)
        else:
            console.print("[red]bridge did not come up within 15s[/red]")
            child.kill()
            raise typer.Exit(1)
        base_url = f"http://127.0.0.1:{actual}/"
        url = f"{base_url}#token={token}"
        if background:
            atomic_write(stopf, str(child.pid))
            atomic_write(discf, str(port_file))
            kept = True
            console.print(Panel(f"[bold green]OhMyLLM console[/bold green] → {base_url}\n"
                                f"[dim]native app not installed · loopback only · `oml gui --web` opens it in a browser · `oml gui --stop` closes it[/dim]",
                                border_style="dim"))
            console.print("[dim]to get the native app: build `gui/src-tauri` (cargo build --release) or set OMLLM_GUI_BIN.[/dim]")
            return
        if no_open:
            # K36: --no-open must hand over the FULL tokenized URL — a bare
            # loopback URL lands on a 401 page with no way to authenticate.
            console.print(Panel(f"[bold green]OhMyLLM console[/bold green] → {url}\n"
                                f"[dim]loopback only · this URL carries the capability token — keep it private · Ctrl+C closes the session[/dim]",
                                border_style="dim"))
        else:
            console.print(Panel(f"[bold green]OhMyLLM console[/bold green] → {base_url}\n"
                                f"[dim]loopback only · capability token is never printed · Ctrl+C closes the session[/dim]",
                                border_style="dim"))
            webbrowser.open(url)
        while child.poll() is None:
            time.sleep(0.3)
    except KeyboardInterrupt:
        console.print("[dim]closing console…[/dim]")
    finally:
        if not kept:
            if child.poll() is None:
                child.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=4)
            _cleanup_bridge_discovery(launch_dir, port_file)


# ------------------------------------------------------------------ helpers
def _bridge_environment(workspace: str | None = None) -> dict[str, str]:
    extra = {"OMLLM_PYTHON": sys.executable}
    for key in ("OMLLM_HOME", "OMLLM_WORKSPACE"):
        if value := os.environ.get(key):
            extra[key] = value
    if gui_bin := os.environ.get("OMLLM_GUI_BIN"):
        if validated := trusted_executable(gui_bin):
            extra["OMLLM_GUI_BIN"] = validated
    if workspace:
        extra["OMLLM_WORKSPACE"] = str(Path(workspace).expanduser().resolve())
    return sanitized_environment(extra)


def _new_bridge_discovery(prefix: str) -> tuple[Path, Path]:
    launch_dir = private_temp_dir(paths.global_home() / "bridge-launches", prefix)
    return launch_dir, launch_dir / "bridge.json"


def _cleanup_bridge_discovery(launch_dir: Path, discovery: Path) -> None:
    with contextlib.suppress(OSError):
        discovery.unlink()
    with contextlib.suppress(OSError):
        launch_dir.rmdir()


def _resolve_native_binary(env: dict[str, str]) -> Path | None:
    """App-first native shell resolution: override, shipped binary, repo build, PATH."""
    cand: list[Path] = []
    if v := env.get("OMLLM_GUI_BIN"):
        cand.append(Path(v))
    cand.append(Path(__file__).resolve().parent / "gui_bin" / "ohmyllm-gui")
    here = Path(__file__).resolve()
    for base in here.parents:
        cand.append(base / "gui" / "src-tauri" / "target" / "release" / "ohmyllm-gui")
        if (base / ".git").exists():
            break
    cand.append(Path.home() / ".local" / "bin" / "ohmyllm-gui")
    if executable := trusted_executable("ohmyllm-gui"):
        cand.append(Path(executable))
    return next((c for c in cand if c.is_file() and os.access(c, os.X_OK)), None)


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


def _read_secret(label: str) -> str:
    """Read a secret without echoing — via getpass, which (unlike click's
    raw-char hidden prompt) lets terminals paste normally. Google codes are
    shown as 'xxxx xxxx xxxx xxxx'; spaces and dashes are cosmetic."""
    import getpass

    try:
        raw = getpass.getpass(f"{label}: ")
    except (EOFError, KeyboardInterrupt):
        return ""
    return raw.strip().replace(" ", "").replace("-", "")


@mail_app.command("setup")
def mail_setup(
    address: str = typer.Option(None, "--address", "-a", help="your email address"),
    password: str = typer.Option(None, "--password", "-p", help="app password (prompted hidden if omitted)"),
    imap: str = typer.Option(None, "--imap", help="override auto-detected IMAP host"),
    smtp: str = typer.Option(None, "--smtp", help="override auto-detected SMTP host"),
) -> None:
    """Connect an email account: auto-detects providers, verifies the login, saves to config."""
    from .config import ConfigStore
    from .mail import detect_provider, test_login

    addr = (address or typer.prompt("Email address")).strip()
    if "@" not in addr:
        console.print("[red]that doesn't look like an email address[/red]")
        raise typer.Exit(1)
    dom = addr.rsplit("@", 1)[-1].lower()
    det = detect_provider(addr)
    ih, ip, sh, sp = det if det else (
        (imap or typer.prompt("IMAP host (e.g. imap.example.com)")), 993,
        (smtp or typer.prompt("SMTP host (e.g. smtp.example.com)")), 465)
    if imap:
        ih = imap
    if smtp:
        sh = smtp
    if det:
        console.print(f"[cyan]{dom}[/cyan] detected — IMAP {ih}:{ip} · SMTP {sh}:{sp}")
    else:
        console.print(f"[yellow]no auto-detection for {dom}[/yellow] — using the hosts above.")
    env_pw = os.environ.get("OMLLM_KEY_MAIL")
    pw = password
    if pw is None and env_pw:
        console.print("[dim]using the OMLLM_KEY_MAIL password from your environment[/dim]")
        pw = env_pw
    if pw is None:
        from .mail import app_password_help, app_password_url
        console.print(f"[cyan]{app_password_help(dom)}[/cyan]")
        url = app_password_url(dom)
        if url:
            import webbrowser

            console.print(f"[dim]opening {url} in your browser…[/dim]")
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        for attempt in (1, 2):
            console.print("[bold]Paste the 16-character code below and press Enter.[/bold] "
                          "Nothing shows on screen while you paste — that is normal, "
                          "it IS being typed. Right-click or Ctrl+Shift+V pastes.")
            pw = _read_secret("App password")
            if pw:
                break
            console.print("[yellow]empty — nothing was pasted. try once more.[/yellow]"
                          if attempt == 1 else "[red]still empty — run [b]oml mail[/b] again when ready[/red]")
        if not pw:
            raise typer.Exit(1)
    store = paths.config_path()
    cfg = ConfigStore(store).load()
    m = cfg.mail.model_copy(update={
        "address": addr, "user": addr, "password": pw,
        "imap_host": ih, "imap_port": ip, "smtp_host": sh, "smtp_port": sp,
        "enabled": True,
    })
    ok, note = test_login(m)
    if not ok:
        console.print(f"[red]{note}[/red]")
        if "proton" in dom:
            console.print("[dim]Proton Mail needs Proton Bridge running (local IMAP on 127.0.0.1:1143).[/dim]")
        else:
            from .mail import app_password_help
            console.print(f"[dim]{app_password_help(dom)}[/dim]")
        raise typer.Exit(1)
    ConfigStore(store).save(cfg.model_copy(update={"mail": m}))
    console.print(f"[green]✓ {note}[/green] · [green]account saved[/green] — the agent can now "
                  f"search/read/send email (send stays approval-gated outside full_access).")


@mail_app.command("status")
def mail_status() -> None:
    """Show the configured mail account (password masked)."""
    from .config import ConfigStore

    m = ConfigStore(paths.config_path()).load().mail
    if not m.address:
        console.print("[dim]no account — run: oml mail[/dim]")
        return
    if not m.password and os.environ.get("OMLLM_KEY_MAIL"):
        pw = "OMLLM_KEY_MAIL env"
    elif m.password:
        pw = "•" * 8  # K38: never leak even a prefix of the password
    else:
        pw = "unset"
    console.print(f"enabled={m.enabled} · {m.address} · IMAP {m.imap_host}:{m.imap_port} · "
                  f"SMTP {m.smtp_host}:{m.smtp_port} · password: {pw}")
    if m.recipient_allowlist:
        console.print(f"recipient allowlist: {m.recipient_allowlist}")


@mail_app.command("test")
def mail_test() -> None:
    """Verify the IMAP login still works."""
    from .config import ConfigStore
    from .mail import test_login

    m = ConfigStore(paths.config_path()).load().mail
    if not m.address:
        console.print("[dim]no account — run: oml mail[/dim]")
        raise typer.Exit(1)
    ok, note = test_login(m)
    console.print(f"[{'green' if ok else 'red'}]{note}[/{'green' if ok else 'red'}]")
    raise typer.Exit(0 if ok else 1)


def app_entry() -> None:  # console_scripts target
    app()


if __name__ == "__main__":
    app_entry()
