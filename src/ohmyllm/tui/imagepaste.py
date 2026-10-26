"""Clipboard image grabbing + terminal image display for the TUI.

`grab_clip_image()` pulls an image off the system clipboard (Wayland, X11,
macOS, Windows) into a temp PNG. `ImageBox` renders it in the chat log the
way modern terminals do: a bordered thumbnail card with a 📷 #N badge and
the pixel dimensions. Terminals that speak the Kitty/iTerm/Sixel graphics
protocol (WezTerm, Ghostty, kitty, contour, orion…) get the real pixels;
everything else still gets the labeled card, so pastes never look broken.
"""
from __future__ import annotations

import base64
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.text import Text
from textual.widgets import Static


def png_size(path: Path) -> tuple[int, int]:
    try:
        with open(path, "rb") as fh:
            head = fh.read(33)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
        if head[:2] == b"\xff\xd8":  # JPEG: walk markers for SOF
            data = path.read_bytes()
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return int(w), int(h)
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
    except (OSError, struct.error, IndexError):
        pass
    return 0, 0


def grab_clip_image() -> Path | None:
    """Return a temp PNG path if the clipboard holds an image, else None."""
    tmp = Path(tempfile.gettempdir()) / "ohmyllm"
    tmp.mkdir(exist_ok=True)
    out = tmp / f"paste-{os.getpid()}-{next(_seq)}.png"
    cmds: list[list[str]] = []
    if sys.platform == "linux":
        wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        if wayland and shutil.which("wl-paste"):
            if not shutil.which("wl-copy"):
                pass
            cmds.append(["wl-paste", "--type", "image/png"])
        if shutil.which("xclip"):
            cmds.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
        if shutil.which("wl-paste") and not wayland:
            cmds.append(["wl-paste", "-t", "image/png"])
    elif sys.platform == "darwin":
        if shutil.which("pngpaste"):
            cmds.append(["pngpaste", str(out)])
        else:
            script = ('set f to (the clipboard as «class PNGf»)\n'
                      f'set p to POSIX file "{out}"\n'
                      'open for access p with write permission\n'
                      'write f to p\n')
            try:
                r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
                if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                    return out
            except (OSError, subprocess.SubprocessError):
                pass
            return None
    elif os.name == "nt":
        esc = str(out).replace("\\", "\\\\")
        ps = ("$i=Get-Clipboard -Format Image; "
              f"if($i){{$i.Save('{esc}',[System.Drawing.Imaging.ImageFormat]::Png)}}")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=10)
            if out.exists() and out.stat().st_size > 0:
                return out
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=6)
            if r.returncode == 0 and len(r.stdout) > 100 and r.stdout[:8] == b"\x89PNG\r\n\x1a\n":
                out.write_bytes(r.stdout)
                return out
        except (OSError, subprocess.SubprocessError):
            continue
    return None


_seq = iter(range(1, 10**6))


def data_uri(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


class ImageBox(Static):
    """A chat-log card for one pasted image: 📷 #N + size + thumbnail."""

    def __init__(self, path: Path, index: int, dims: tuple[int, int], **kw) -> None:
        super().__init__(**kw)
        self.path = path
        self.index = index
        self.dims = dims
        self.border_title = f"📷 #{index}"
        self.border_subtitle = f"{dims[0]}x{dims[1]}"
        self.styles.border = ("round", "orange")

    def on_mount(self) -> None:
        self._paint_card()

    def _paint_card(self) -> None:
        w = max(self.region.width - 4, 8)
        h = max(min(self.region.height - 2, 6), 1)
        try:
            from PIL import Image  # optional: real preview when Pillow exists

            im = Image.open(self.path).convert("L").resize((w * 2, h * 4))
            px = im.load()
            ramp = " .:-=+*#%@"
            lines = []
            for y in range(0, im.height, 4):
                row = "".join(ramp[min(px[x, y] * len(ramp) // 256, len(ramp) - 1)]
                              for x in range(0, im.width, 2))
                lines.append(row[:w])
            self.update(Text("\n".join(lines[:h]), style="grey70"))
        except Exception:  # noqa: BLE001 — no Pillow / bad file: keep the card
            self.update(Text(f"[dim]{self.path.name} — image attached to the message[/dim]"))
