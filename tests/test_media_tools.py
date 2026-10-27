"""Multimodal read_file: images, audio, video — and the runtime's vision gate."""

from __future__ import annotations

import asyncio
import json

from ohmyllm import media
from ohmyllm.agents import AgentRuntime
from ohmyllm.tools import ToolContext
from ohmyllm.tools.file_tools import read_file

# a small PNG (valid header; payload bytes are never decoded by the tool)
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000008000000080802000000fe4f62"
    "e0000000164944415408d763f8ffff3f030d00000000ffff03e8013a642a6c00"
    "00000049454e44ae426082"
)


def _ctx(svc, ws):
    return ToolContext(workspace=ws, mode="sandboxed", config=svc.config)


# ------------------------------------------------------------------ tool
def test_read_file_image_returns_data_url(oml_env, svc):
    ws = oml_env["ws"]
    (ws / "x.png").write_bytes(PNG)
    res = asyncio.run(read_file(_ctx(svc, ws), {"path": "x.png"}))
    assert res.ok
    assert res.meta["media"] == "image"
    assert res.meta["images"][0].startswith("data:image/png;base64,")


def test_read_file_audio_without_engine_is_honest(oml_env, svc, monkeypatch):
    from ohmyllm import voice

    monkeypatch.setattr(voice, "pick_engine", lambda lang=None: (None, "no STT model"))
    ws = oml_env["ws"]
    (ws / "speech.mp3").write_bytes(b"ID3\x03not-really-mp3")
    res = asyncio.run(read_file(_ctx(svc, ws), {"path": "speech.mp3"}))
    assert not res.ok
    assert "speech-to-text" in (res.error or "").lower()
    assert "no STT model" in (res.error or "")


def test_read_file_video_without_ffmpeg_is_honest(oml_env, svc, monkeypatch):
    monkeypatch.setattr(media, "_ffmpeg", lambda: None)
    ws = oml_env["ws"]
    (ws / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    res = asyncio.run(read_file(_ctx(svc, ws), {"path": "clip.mp4"}))
    assert not res.ok
    assert "ffmpeg" in (res.error or "").lower()


def test_read_file_binary_rejected_cleanly(oml_env, svc):
    ws = oml_env["ws"]
    (ws / "blob.bin").write_bytes(b"\x7fELF\x00\x01\x02\x00binary")
    res = asyncio.run(read_file(_ctx(svc, ws), {"path": "blob.bin"}))
    assert not res.ok
    assert "binary" in (res.error or "").lower()


# ------------------------------------------------------------- vision gate
def _run_read_turn(svc, ws, monkeypatch, verdict):
    from ohmyllm import vision

    monkeypatch.setattr(vision, "cached_verdict", lambda be, model: verdict)
    rt = AgentRuntime(
        backend=svc.backend(), registry=svc.registry, policy=svc.policy,
        approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul,
    )
    sess = svc.new_session()
    events: list[dict] = []
    res = asyncio.run(rt.run_turn(
        sess, "please read the file x.png and describe it",
        on_event=events.append, model="mock-tool-7b",
    ))
    assert res.error is None
    return sess, events


def test_runtime_attaches_images_when_model_can_see(oml_env, svc, monkeypatch):
    ws = oml_env["ws"]
    (ws / "x.png").write_bytes(PNG)
    sess, events = _run_read_turn(svc, ws, monkeypatch, True)
    media_msgs = [
        m for m in sess.messages
        if m.get("role") == "user" and isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
    ]
    assert media_msgs, "vision-capable model must receive the image as content parts"
    url = media_msgs[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # UI events must never carry the base64 payload itself
    tr = [e for e in events if e.get("type") == "tool_result" and e.get("file")]
    assert tr and tr[-1]["file"]["images"] == 1
    assert "base64" not in json.dumps(tr[-1])


def test_runtime_notes_blind_model_without_breaking_turn(oml_env, svc, monkeypatch):
    ws = oml_env["ws"]
    (ws / "x.png").write_bytes(PNG)
    sess, _events = _run_read_turn(svc, ws, monkeypatch, False)
    tool_msgs = [m for m in sess.messages if m.get("role") == "tool"]
    assert tool_msgs and "cannot see images" in str(tool_msgs[-1]["content"])
    assert not any(
        m.get("role") == "user" and isinstance(m.get("content"), list)
        for m in sess.messages
    ), "non-vision model must not be sent image parts"
