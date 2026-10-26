"""Definitive vision testing: prove a model can see, or prove it can't.

The only honest guarantee is a test the model cannot pass by guessing:
we render a fresh 5-digit number into a PNG, send it, and demand the
digits back. A text-only model has a 1-in-100,000 chance of matching;
we require two independent matches before saying "yes". A server error
about images says "no" instantly. Results are cached forever in
~/.ohmyllm/vision.json — probe once, know forever, no "?" in the UI.
"""
from __future__ import annotations

import base64
import io
import json
import random
import re
from pathlib import Path
from typing import Any

from .paths import global_home


# 5x7 bitmap font for the Pillow-free fallback path (digits only)
_GLYPHS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("01110", "10000", "10111", "10101", "10101", "10101", "01111"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01111", "10001", "10001", "10001", "01111", "00001", "01110"),
}


def _png_with_digits_native(digits: str, scale: int = 20) -> bytes:
    """Pure-Python PNG: white canvas, black 5x7 glyphs scaled up — readable
    by any vision encoder, and it works without Pillow (CI, slim installs)."""
    import struct as _struct
    import zlib as _zlib

    w, h = (len(digits) * 6 - 1) * scale, 7 * scale
    rows = []
    for y in range(h):
        band = bytearray([0])  # filter byte
        line = []
        gy = y // scale
        for x in range(w):
            gx = x // scale
            ch_i = gx // 6
            bit = 0
            if ch_i < len(digits) and gx % 6 < 5:
                g = _GLYPHS.get(digits[ch_i])
                if g:
                    bit = int(g[gy][gx % 6])
            line.append(0 if bit else 255)
        band += bytes(line)
        rows.append(bytes(band))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (_struct.pack(">I", len(data)) + tag + data
                + _struct.pack(">I", _zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", _struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", _zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _png_with_digits(digits: str) -> bytes:
    """Render `digits` huge and high-contrast — readable by even weak ViTs.
    Pillow when available (crisper), pure-Python bitmap PNG otherwise."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        w, h = 640, 200
        img = Image.new("L", (w, h), 255)
        d = ImageDraw.Draw(img)
        font: Any
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 150)
        except OSError:
            font = ImageFont.load_default(150)
        d.text((w / 2, h / 2), digits, fill=0, font=font, anchor="mm")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return _png_with_digits_native(digits)


def probe_image_b64(digits: str) -> str:
    return base64.b64encode(_png_with_digits(digits)).decode()


# ------------------------------------------------------------------ cache
def _cache_file() -> Path:
    return global_home() / "vision.json"


def load_cache() -> dict[str, bool]:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_verdict(model_key: str, can_see: bool) -> None:
    data = load_cache()
    if data.get(model_key) is can_see:
        return
    data[model_key] = can_see
    try:
        _cache_file().parent.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def cached_verdict(backend: str, model: str) -> bool | None:
    return load_cache().get(f"{backend}:{model}")


# ------------------------------------------------------------------- probe
_DIGITS_RE = re.compile(r"\d+")
_IMG_ERROR_RE = re.compile(r"(image|vision|multimodal|modality)", re.I)


async def probe_vision(backend: Any, model: str, *, timeout: float = 120.0) -> bool:
    """True only if the model reads two fresh random numbers correctly.

    Raises nothing: any image-rejection error returns False (that IS the
    negative proof); stream/timeout failures also settle to False but the
    caller may retry later.
    """
    from .config.models import GenerationSettings

    params = GenerationSettings(max_tokens=400, temperature=0.0, effort="off")
    ok = fail = 0
    for _ in range(3):
        digits = f"{random.randint(10000, 99999)}"
        parts = [
            {"type": "text", "text": "Reply with ONLY the digits shown in this image, nothing else."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{probe_image_b64(digits)}"}},
        ]
        text = ""
        try:
            async for ch in backend.stream_chat(model, [{"role": "user", "content": parts}], params):
                text += ch.content or ""
        except Exception as exc:  # noqa: BLE001 — server errors are evidence
            msg = str(exc)
            if _IMG_ERROR_RE.search(msg) and ("not" in msg.lower() or "unsupport" in msg.lower()
                                              or "reject" in msg.lower() or "invalid" in msg.lower()):
                return False  # explicit image rejection = definitive no
            # generic failure: can't conclude — try once more, then no
            continue
        found = _DIGITS_RE.findall(text)
        if digits in found or (len(found) >= 5 and "".join(found)[:5] == digits):
            ok += 1
        else:
            fail += 1
        if ok >= 2:
            return True     # two independent reads: it can see
        if fail >= 2:
            return False    # two misses: it cannot (or cannot OCR — unusable either way)
    return fail > ok        # 1-1 after attempts: conservative no


def verdict_label(v: bool | None) -> str:
    return {True: "yes", False: "no", None: "not tested"}[v]
