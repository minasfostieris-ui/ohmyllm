"""ohmyllm doctor — environment diagnostics."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import __version__, paths
from ..backends.lmstudio import LMStudioBackend
from ..backends.ollama import OllamaBackend
from ..config import Config, ConfigStore
from ..config.models import PermissionMode
from ..vram.detect import cpu_ram_bytes, detect_gpus, human_bytes

STATUS_ICONS = {"pass": "✔", "warn": "⚠", "fail": "✖", "info": "•"}


@dataclass
class Check:
    id: str
    label: str
    status: str  # pass|warn|fail|info
    detail: str = ""
    fix: str = ""

    @property
    def icon(self) -> str:
        return STATUS_ICONS.get(self.status, "•")


def run_checks(store: ConfigStore | None = None, config: Config | None = None) -> list[Check]:
    store = store or ConfigStore()
    checks: list[Check] = []
    _add = checks.append

    # python
    ver = sys.version_info
    if ver >= (3, 11):
        _add(Check("python", "Python runtime", "pass", f"{sys.version.split()[0]}"))
    else:
        _add(Check("python", "Python runtime", "fail", f"{sys.version.split()[0]} < 3.11", "Install Python 3.11+ and reinstall."))

    # package + entrypoints
    _add(Check("package", "OhMyLLM package", "pass", f"v{__version__} at {Path(__file__).parent}"))
    for cmd in ("ohmyllm", "omllm", "OMLLM"):
        found = shutil.which(cmd)
        if found:
            _add(Check(f"cmd-{cmd}", f"command '{cmd}' on PATH", "pass", found))
        else:
            _add(Check(
                f"cmd-{cmd}", f"command '{cmd}' on PATH", "warn", "not found",
                "Run: ohmyllm self-install (or re-run scripts/install.sh / install.ps1)",
            ))

    # config
    if store.exists():
        cfg = config or store.load()
        problems = store.last_problems
        _add(Check(
            "config", f"config.toml ({store.path})", "warn" if problems else "pass",
            "; ".join(problems) or "valid",
            "Fix the file manually or run: ohmyllm init --force" if problems else "",
        ))
    else:
        cfg = config or Config()
        _add(Check("config", "config.toml", "warn", "missing — defaults in use", "Run: ohmyllm init (or launch ohmyllm once)"))

    # directories
    for name, d in (
        ("home", paths.global_home()),
        ("logs", paths.logs_dir()),
        ("backups", paths.backups_dir()),
        ("history", paths.history_dir()),
    ):
        ok = _writable(d)
        _add(Check(f"dir-{name}", f"{name} dir ({d})", "pass" if ok else "fail", "" if ok else "not writable", f"Permissions on {d}"))

    # soul
    soul = paths.soul_path()
    if soul.is_file():
        _add(Check("soul", f"soul.md ({soul})", "pass", "exists" if _writable(soul.parent) else "exists, NOT writable"))
    else:
        _add(Check("soul", f"soul.md ({soul})", "warn", "missing", "Launch ohmyllm once or run: ohmyllm init"))

    # permission mode
    valid_modes = {m.value for m in PermissionMode}
    _add(Check("mode", "permission mode", "pass" if cfg.permission_mode.value in valid_modes else "fail", cfg.permission_mode.value))
    _add(Check("backend", "default backend", "pass", cfg.default_backend.value))

    # ollama
    ob = OllamaBackend(cfg)
    if ob.is_installed():
        _add(Check("ollama-binary", "ollama binary", "pass", ob.binary() or "?"))
        try:
            with httpx.Client(timeout=2.0, base_url=cfg.ollama.host) as c:
                r = c.get("/api/version")
                if r.status_code == 200:
                    n = len(c.get("/api/tags").json().get("models", []))
                    _add(Check("ollama-server", "Ollama server", "pass", f"v{r.json().get('version')} @ {cfg.ollama.host}, {n} model(s)"))
                    if n == 0:
                        _add(Check("ollama-models", "Ollama models", "warn", "no models", "Pull one:  ollama pull qwen2.5:7b-instruct"))
                else:
                    _add(Check("ollama-server", "Ollama server", "fail", f"HTTP {r.status_code}", "Run: ollama serve"))
        except Exception as exc:  # noqa: BLE001
            _add(Check("ollama-server", "Ollama server", "warn", f"not reachable: {type(exc).__name__}", "Start it (Settings → auto-start will try)"))
    else:
        _add(Check(
            "ollama-binary", "ollama binary", "info", "not installed",
            "Install: https://ollama.com/download  ·  Linux: curl -fsSL https://ollama.com/install.sh | sh",
        ))

    # lm studio
    lb = LMStudioBackend(cfg)
    if lb.is_installed():
        _add(Check("lmstudio-app", "LM Studio", "pass", "found"))
    try:
        with httpx.Client(timeout=2.0, base_url=cfg.lmstudio.base_url) as c:
            r = c.get("/v1/models")
            if r.status_code == 200:
                n = len(r.json().get("data", []))
                _add(Check("lmstudio-server", "LM Studio server", "pass", f"{cfg.lmstudio.base_url}, {n} model(s)"))
            else:
                _add(Check("lmstudio-server", "LM Studio server", "warn", f"HTTP {r.status_code}"))
    except Exception:  # noqa: BLE001
        _add(Check(
            "lmstudio-server", "LM Studio server", "info", "not running" + ("" if lb.is_installed() else " (app not detected)"),
            "Open LM Studio → Developer/Server tab → Start Server (port 1234), or run `lms server start`",
        ))

    # gpu
    gpus = detect_gpus()
    if gpus:
        for g in gpus:
            _add(Check("gpu", "GPU detected", "pass", f"{g.model}: {human_bytes(g.total_bytes)} total" + (f", {human_bytes(g.free_bytes)} free" if g.free_bytes is not None else "") + f" ({g.source})"))
    else:
        _add(Check("gpu", "GPU detected", "warn", "none visible — CPU inference will be slow for agents", "Smaller quantizations (Q3/Q4) help on CPU/RAM"))
    ram = cpu_ram_bytes()
    if ram:
        _add(Check("ram", "System RAM", "pass", human_bytes(ram)))

    # active model sanity
    active = getattr(cfg, cfg.default_backend.value, None)
    dm = getattr(active, "default_model", None) if active else None
    if cfg.default_backend.value == "mock":
        _add(Check("model", "default model", "pass", "mock-tool-7b (dev backend)"))
    elif dm:
        _add(Check("model", "default model", "info", dm))
    else:
        _add(Check("model", "default model", "warn", "not configured", "Pick one in the Models screen (Ctrl+M)"))

    workspace = Path(cfg.workspace) if cfg.workspace else paths.workspace_root()
    _add(Check("workspace", "workspace", "pass" if workspace.is_dir() else "fail", str(workspace)))

    # audit
    audit = paths.audit_log_path()
    if audit.is_file():
        n = sum(1 for _ in open(audit, encoding="utf-8", errors="replace"))
        _add(Check("audit", "audit log", "pass", f"{n} event(s) @ {audit}"))
    else:
        _add(Check("audit", "audit log", "info", "no events yet", ""))

    # danger guard (only probed when enabled)
    if cfg.guard.enabled:
        import json as _json
        import urllib.request

        body = _json.dumps({
            "model": cfg.guard.model, "state": "ping",
            "questions": {"dangerous": {"type": "noul", "instructions": "Is this text dangerous?"}},
        }).encode()
        req = urllib.request.Request(
            cfg.guard.base_url.rstrip("/") + "/v1/systemone", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                ans = (_json.loads(r.read()).get("answers") or {}).get("dangerous") or {}
            if "noul" in ans:
                _add(Check("guard", "danger guard (Clef-Flash)", "pass",
                           f"{cfg.guard.model} @ {cfg.guard.base_url} (CPU decision server)"))
            else:
                _add(Check("guard", "danger guard (Clef-Flash)", "warn",
                           "server answered but no noul result",
                           "Check llama-server -ngl 0 --port … (systemone endpoint)"))
        except Exception as exc:  # noqa: BLE001
            _add(Check("guard", "danger guard (Clef-Flash)", "fail",
                       f"{cfg.guard.base_url} unreachable: {type(exc).__name__}",
                       "Start it CPU-only: llama-server -hf ggml-org/Clef-Flash-GGUF -ngl 0 --port 9931"))
    return checks


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".omllm-write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return os.access(d, os.W_OK) if d.exists() else False


def summarize(checks: list[Check]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in checks:
        out[c.status] = out.get(c.status, 0) + 1
    return out
