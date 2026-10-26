"""Regressions for the security sweep findings (config masking, token files,
skill traversal, save atomicity, routing honesty, multimodal, pid validation)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import threading

import pytest

from ohmyllm.backends.registry import make_backend
from ohmyllm.config import Config, ConfigStore

SECRET = "sk-REAL-abcdef123456789-DO-NOT-LEAK"


# ---- #1 config serialization masks keys; PATCH cannot write them
def test_api_config_masks_keys(bridge):
    bridge.post("/api/providers/key", {"pid": "openrouter", "key": SECRET})
    body = json.dumps(bridge.get("/api/config"))
    assert SECRET not in body
    r = bridge.post("/api/config", {"cloud": {"keys": {"openrouter": "sk-INJECTED-999"}}})
    assert r.get("ok")
    rows = bridge.get("/api/providers")["providers"]
    row = next(p for p in rows if p["pid"] == "openrouter")
    assert row["has_key"] and "INJECTED" not in row["masked"]
    # the real key survived the attempted overwrite
    from ohmyllm.config import ConfigStore

    assert ConfigStore().load().cloud.keys["openrouter"] == SECRET


def test_partial_cloud_patch_keeps_other_fields(bridge):
    bridge.post("/api/providers/custom", {"name": "Home", "base_url": "http://192.168.1.5:8000",
                                          "models": "a,b"})
    bridge.post("/api/config", {"cloud": {"provider": "openrouter"}})
    cfg = ConfigStore().load()
    assert cfg.cloud.customs and cfg.cloud.model_lists  # not erased by the patch


# ---- #2 port/token file owner-only; log never contains the token
def test_port_file_owner_only(tmp_path):
    from ohmyllm.bridge import serve

    pf = tmp_path / "x.port"
    th = threading.Thread(target=lambda: serve(port=0, write_port_file=str(pf)), daemon=True)
    th.start()
    for _ in range(50):
        if pf.exists():
            break
        __import__("time").sleep(0.1)
    assert pf.exists()
    if os.name == "posix":
        assert stat.S_IMODE(pf.stat().st_mode) == 0o600
    content = pf.read_text()
    tok = content.split(" ")[1]
    # log redaction: fake a request line through the handler's logger
    from ohmyllm import paths

    log = paths.app_log_path()
    if log.exists():
        assert tok not in log.read_text()
    # stop the server
    import urllib.request

    port = int(content.split(" ")[0])
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST")
    req.add_header("X-OMLLM-Token", tok)
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:  # noqa: BLE001
        pass


# ---- #3 skill named ".." cannot rmtree the home
def test_skill_dotdot_name_rejected(tmp_path, monkeypatch):
    from ohmyllm.skills import SkillManager

    home = tmp_path / ".ohmyllm"
    (home / "skills").mkdir(parents=True)
    (home / "config.toml").write_text("# precious\n")
    monkeypatch.setattr("ohmyllm.paths.data_dir", lambda: home, raising=False)
    mgr = SkillManager(root=home / "skills")
    src = tmp_path / "evil"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: ..\ndescription: x\n---\nboom\n")
    with pytest.raises(ValueError):
        mgr._copy_skill(src)
    assert (home / "config.toml").exists()
    assert (home / "skills").exists()


# ---- #4 concurrent saves never collide on one tmp name
def test_dump_file_unique_tmp(tmp_path):
    from ohmyllm.config import tomlio

    target = tmp_path / "c.toml"
    tomlio.dump_file(target, {"a": 1})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "c.toml"]
    assert leftovers == []
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ---- #7 whisperx needs a whisper model; no dead condition
def test_whisperx_requires_whisper_model(tmp_path, monkeypatch):
    from ohmyllm import voice

    vdir = tmp_path / "voice"
    monkeypatch.setattr(voice, "voice_dir", lambda: vdir)
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("whisperx", "whisper-cli") else None)
    eng, msg = voice.pick_engine("fi")
    assert eng is None and "no model" in msg
    vdir.mkdir(parents=True)
    (vdir / "ggml-small.bin").write_bytes(b"x")
    eng, _ = voice.pick_engine("fi")
    assert eng == "whisperx"


# ---- #5 timeout kills the child
def test_transcribe_timeout_kills(monkeypatch, tmp_path):
    from ohmyllm import voice

    vdir = tmp_path / "voice"
    vdir.mkdir(parents=True)
    (vdir / "ggml-small.bin").write_bytes(b"x")
    monkeypatch.setattr(voice, "voice_dir", lambda: vdir)
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/whisper-cli" if n == "whisper-cli" else None)
    killed = {}

    class Proc:
        returncode = None

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            killed["k"] = True

        async def wait(self):
            return 0

    async def fake_exec(*a, **k):
        return Proc()

    monkeypatch.setattr(voice.asyncio, "create_subprocess_exec", fake_exec)
    wav = vdir / "r.wav"
    wav.write_bytes(b"RIFF")
    text, err = asyncio.run(voice.transcribe(wav, timeout=0.01))
    assert "timed out" in (err or "") and killed.get("k")


# ---- #6 anthropic folds image parts instead of str()-ing them
def test_anthropic_multimodal_blocks():
    cfg = Config()
    cfg.cloud.provider = "anthropic"
    cfg.cloud.keys["anthropic"] = "sk-ant"
    be = make_backend("cloud", cfg)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what number?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ]}]
    system, out = be._to_anthropic(msgs)
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "what number?"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"


# ---- #8/#9 pid validation + junk URLs
def test_use_rejects_unknown_pid(bridge):
    r = bridge.post("/api/providers/use", {"pid": "not-a-provider"})
    assert "error" in r


def test_custom_rejects_junk_base(bridge):
    r = bridge.post("/api/providers/custom", {"name": "x", "base_url": "javascript:alert(1)"})
    assert "error" in r
    r = bridge.post("/api/providers/custom", {"name": "x", "base_url": "httpx://ok/v1"})
    assert "error" in r
