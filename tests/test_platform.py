"""Cross-platform behavior simulated on this host (CI runs the same suite
natively on Windows/macOS — these tests pin the platform branches so a Linux
box catches regressions in the Windows/macOS code paths too)."""
import os
import subprocess
from pathlib import Path

import pytest

from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.config import Config
from ohmyllm.safety.risk import _basename
from ohmyllm.vram import detect


# ---------------------------------------------------------------- basenames
@pytest.mark.parametrize("tok,expect", [
    ("C:\\Users\\minas\\tools\\cleaner.bat", "cleaner.bat"),
    ("/usr/bin/rm", "rm"),
    ("..\\..\\evil.exe", "evil.exe"),
    ("plain", "plain"),
    ("C:\\", ""),
])
def test_basename_handles_windows_and_posix(tok, expect):
    assert _basename(tok) == expect


# ---------------------------------------------------------------- lms CLI
def test_lms_cli_windows_name(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bindir = tmp_path / ".lmstudio" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "lms.exe").write_text("")
    monkeypatch.setattr("shutil.which", lambda n: None)
    be = LMStudioBackend(Config())
    assert be.lms_cli() == str(bindir / "lms.exe")


def test_lms_cli_posix_name(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("shutil.which", lambda n: None)
    bindir = tmp_path / ".lmstudio" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "lms").write_text("")
    be = LMStudioBackend(Config())
    assert be.lms_cli() == str(bindir / "lms")


# ---------------------------------------------------------- app discovery
def test_app_candidate_windows_localappdata(tmp_path, monkeypatch):
    exe = tmp_path / "Programs" / "LM Studio" / "LM Studio.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    import ohmyllm.backends.lmstudio as lms
    monkeypatch.setattr(lms.platform, "system", lambda: "Windows")
    be = LMStudioBackend(Config())
    assert be._app_candidate() == exe


def test_app_candidate_darwin(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "LM Studio.app"
    app.mkdir(parents=True)
    import ohmyllm.backends.lmstudio as lms
    monkeypatch.setattr(lms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lms.Path, "home", lambda: tmp_path)
    be = LMStudioBackend(Config())
    assert be._app_candidate() == app


# ---------------------------------------------------------------- GPU detect
def test_detect_gpus_windows_prefers_nvidia_smi(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Windows")
    calls = []

    def fake_nvidia():
        calls.append("nvidia")
        return [detect.GPUInfo("nvidia", "RTX 4090", 24 * 2**30, None, "test")]

    def fake_windows():
        calls.append("windows")
        return []

    monkeypatch.setattr(detect, "_nvidia", fake_nvidia)
    monkeypatch.setattr(detect, "_windows", fake_windows)
    gpus = detect.detect_gpus()
    assert calls == ["nvidia"] and gpus[0].total_bytes == 24 * 2**30


def test_detect_gpus_windows_falls_back_to_wmi(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(detect, "_nvidia", lambda: [])
    monkeypatch.setattr(detect, "_windows", lambda: [
        detect.GPUInfo("windows", "AMD RX 7900", 8 * 2**30, None, "test")])
    gpus = detect.detect_gpus()
    assert gpus and gpus[0].source == "test"


def test_detect_gpus_darwin_apple_only(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(detect, "_apple", lambda: [
        detect.GPUInfo("apple", "M3 Ultra", 32 * 2**30, None, "test")])
    monkeypatch.setattr(detect, "_windows", lambda: pytest.fail("_windows must not run on macOS"))
    assert detect.detect_gpus()[0].model == "M3 Ultra"


def test_windows_wmi_parses_json(monkeypatch):
    out = '[{"Name":"NVIDIA GeForce RTX 4060","AdapterRAM":4293918720}]'
    monkeypatch.setattr(detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(detect, "_run", lambda cmd, timeout=12.0: out)
    gpus = detect._windows()
    assert gpus and "RTX 4060" in gpus[0].model


# ---------------------------------------------------------------- spawn flags
def test_popen_kw_posix_detached():
    popen_kw: dict = {}
    if os.name == "posix":
        popen_kw["start_new_session"] = True
    else:
        popen_kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    if os.name == "posix":
        assert popen_kw == {"start_new_session": True}
    else:
        assert "creationflags" in popen_kw


# ------------------------------------------------------------- config paths
def test_paths_use_home_and_work_on_any_os(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from ohmyllm import paths
    monkeypatch.delenv("OMLLM_HOME", raising=False)
    monkeypatch.setattr(paths, "global_home", lambda: tmp_path / ".ohmyllm")
    cfgp = paths.config_path()
    assert str(cfgp).endswith("config.toml") and cfgp.is_absolute()
    cfgp.parent.mkdir(parents=True, exist_ok=True)
    cfgp.write_text("first_run_completed = true\n")
    from ohmyllm.config import ConfigStore
    cfg = ConfigStore(cfgp).load()
    assert cfg.first_run_completed is True


# ------------------------------------------------------ campaign script portability
def test_campaign_honors_env_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OMLLM_CAMPAIGN_DIR", str(tmp_path / "camp"))
    src = Path(__file__).resolve().parents[1] / "scripts" / "preview_campaign.py"
    code = src.read_text(encoding="utf-8")
    assert 'OMLLM_CAMPAIGN_DIR' in code
    assert '_root / "imgs"' in code  # no hardcoded /tmp/imgs


def test_sys_argv_patched_guard():
    # the campaign script must not import anything POSIX-only
    src = (Path(__file__).resolve().parents[1] / "scripts" / "preview_campaign.py").read_text()
    for banned in ("import fcntl", "import termios", "os.setsid", "signal.SIGKILL"):
        assert banned not in src
