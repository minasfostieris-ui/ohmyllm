"""Model marketplace: curated recommendations + VRAM fit + downloads.

Everything here is deliberately data-driven: the catalog is a list, the
"will it fit" verdict comes from a best-effort cross-platform VRAM probe
(NVIDIA, AMD, Apple Silicon unified memory), and downloads stream straight
into the LM Studio models folder so the app picks them up automatically.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx


@dataclass(frozen=True)
class MarketEntry:
    mid: str
    title: str
    repo: str          # huggingface repo id
    file: str          # file inside the repo
    size_gb: float
    blurb: str
    short: str = ""  # one-line pitch for the list row
    kind: str = "gguf"  # gguf -> LM Studio folder; ninfer -> own runtime
    companion: str | None = None  # mmproj file needed for vision, same repo
    tags: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.file}"

    @property
    def companion_url(self) -> str | None:
        if not self.companion:
            return None
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.companion}"


# The curated shelf — what the author actually runs. Sizes are the real
# file sizes on the Hugging Face servers (verified 2026-10).
CATALOG: list[MarketEntry] = [
    MarketEntry(
        mid="qwen38-iq2xs",
        title="Qwen3.8-27B · IQ2_XS (GSQ-RCO)",
        repo="ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF",
        file="Qwen3.8-27B-GSQ-RCO-IQ2_XS.gguf",
        size_gb=8.4,
        short="ETH research quant — best 27B per gigabyte, vision-capable",
        blurb="ETH ISTA-DASLab research quant: GSQ (Gumbel-Softmax) learns the "
              "scalar grid per tensor, RCO assigns quant types under a size "
              "budget. At the SAME 8.4 GB it beats Unsloth's dynamic quants by "
              "~7.7% wikitext perplexity — best 27B per gigabyte right now. "
              "Vision-capable (grab the mmproj below it in the repo).",
        companion="mmproj-Qwen3.8-27B-BF16.gguf",
        tags=("thinking", "vision", "27B", "fits 12 GB"),
    ),
    MarketEntry(
        mid="qwen38-q6k",
        title="Qwen3.8-27B · Q6_K (Unsloth)",
        repo="unsloth/Qwen3.8-27B-GGUF",
        file="Qwen3.8-27B-UD-Q6_K.gguf",
        size_gb=22.0,
        short="near-original quality — the safe pick on 24 GB+ cards",
        blurb="Unsloth Dynamic 2.0: near-original quality, the safe choice "
              "when you have the VRAM. ~2.6x the size of IQ2_XS for a few "
              "points of perplexity — pick this for coding/agent work on "
              "24 GB+ cards (3090/4090/5090, Mac 32 GB+).",
        companion="mmproj-F16.gguf",
        tags=("thinking", "vision", "27B", "needs 24 GB"),
    ),
    MarketEntry(
        mid="sushi-9b",
        title="Qwen3.5-9B Sushi-Coder-RL · Q6_K",
        repo="bigatuna/Qwen3.5-9b-Sushi-Coder-RL-GGUF",
        file="Qwen3.5-9b-Sushi-Coder-RL.Q6_K.gguf",
        size_gb=7.4,
        short="RL coder that always thinks — fits 8 GB cards and Macs",
        blurb="RL-tuned coder that always thinks (forced reasoning). Sharp on "
              "refactors and tool calls, small enough for 8-12 GB cards and "
              "Macs. Vision companion: BF16-mmproj in the same repo.",
        companion="Qwen3.5-9b-Sushi-Coder-RL.BF16-mmproj.gguf",
        tags=("forced thinking", "vision", "coder", "fits 8 GB"),
    ),
    MarketEntry(
        mid="gemma4-26b",
        title="Gemma-4-26B-A4B · QAT Q4_0 (NInfer)",
        repo="WaveCut/gemma-4-26B-A4B-it-QAT-Q4_0-MTP-NInfer-v3",
        file="gemma-4-26B-A4B-it-QAT-Q4_0-MTP-ninfer-v3.ninfer",
        size_gb=15.8,
        short="MoE 26B/4B-active, vision, MTP-fast — NInfer runtime only",
        blurb="Mixture-of-Experts (26B total / 4B active) with MTP "
              "speculative decoding — fast like a 4B, smart like a 26B, "
              "vision built in. NOTE: .ninfer format — runs in the NInfer "
              "runtime (RTX-3090 tuned), not LM Studio. Downloaded to the "
              "same folder; point NInfer at it.",
        kind="ninfer",
        tags=("vision", "MoE", "fast", "NInfer runtime"),
    ),
]


# ------------------------------------------------------------------ VRAM
def detect_vram_bytes() -> int | None:
    """Total VRAM / usable unified memory — reuses the vetted cross-platform
    probe (NVIDIA, ROCm, sysfs, Apple Silicon, Windows)."""
    from .vram import total_vram_bytes

    return total_vram_bytes() or None


def vram_gib(total: int | None) -> int | None:
    """Marketing-rounded GiB — 12868124672 bytes is a 12 GB card, not 13."""
    if total is None:
        return None
    return round(total / 2**30)


def vram_label(total: int | None) -> str:
    g = vram_gib(total)
    if g is None:
        return "VRAM unknown"
    return f"{g} GB VRAM"


def fit_verdict(size_gb: float, vram_bytes: int | None) -> tuple[str, str]:
    """(key, label) — key in {fits, tight, no, unknown}."""
    if vram_bytes is None:
        return "unknown", "size ?"
    vram_gb = float(vram_gib(vram_bytes) or 0)
    if size_gb <= vram_gb * 0.80:
        return "fits", f"fits {vram_gb:.0f} GB"
    if size_gb <= vram_gb * 0.95:
        return "tight", f"tight on {vram_gb:.0f} GB"
    return "no", f"won't fit {vram_gb:.0f} GB"


# ------------------------------------------------------------- lm studio dir
def lmstudio_models_dir() -> Path:
    env = os.environ.get("LMSTUDIO_MODEL_PATH") or os.environ.get("LMSTUDIO_MODELS_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".lmstudio" / "models"


def download_dest(entry: MarketEntry) -> Path:
    sub = "ninfer" if entry.kind != "gguf" else "ohmyllm-market"
    return lmstudio_models_dir() / sub


# --------------------------------------------------------------- vision scan
_MM_RE = re.compile(r"mmproj", re.IGNORECASE)


def scan_vision_models(root: Path | None = None) -> set[str]:
    """Model names (lowercase) that have an mmproj companion file on disk.

    LM Studio's API doesn't flag vision, but multimodal GGUFs always ship a
    sidecar `mmproj-*.gguf` / `*-mmproj.gguf` next to the weights — so the
    filesystem is the ground truth.
    """
    root = root or lmstudio_models_dir()
    out: set[str] = set()
    if not root.is_dir():
        return out
    try:
        for d in {p.parent for p in root.rglob("*.gguf")}:
            files = list(d.glob("*.gguf"))
            if not any(_MM_RE.search(f.name) for f in files):
                continue
            for f in files:
                if _MM_RE.search(f.name):
                    continue
                out.add(_name_from_file(f.stem))
    except OSError:
        pass
    return out


def _name_from_file(stem: str) -> str:
    s = re.sub(r"\.(Q\d[^.]*|IQ\d[^.]*|BF16|F16|F32)$", "", stem, flags=re.IGNORECASE)
    s = re.sub(r"[-_. ](bf16|f16|q\d[^\s]*|iq\d[^\s]*)$", "", s, flags=re.IGNORECASE)
    return s.lower()


def has_vision(model_name: str, scanned: set[str]) -> bool | None:
    """True/False from the disk scan, else name heuristic, else None (unknown)."""
    low = model_name.lower()
    for s in scanned:
        # fuzzy: either contains the other's distinctive prefix
        if s and (s in low or low in s):
            return True
    if any(h in low for h in ("vl-", "vision", "moondream", "gemma-4", "gemma4",
                              "llava", "qwen-vl", "pixtral", "nanbeier")):
        return True
    if re.search(r"(llama-[23]|qwen2\.5-|gpt2|opt-125m|phi-3-mini$|ornith|dolphin)", low):
        return False
    return None


# ---------------------------------------------------------------- download
@dataclass
class DownloadState:
    entry: MarketEntry
    received: int = 0
    total: int = 0
    mbps: float = 0.0
    eta_s: float = 0.0
    done: bool = False
    error: str | None = None
    dest: Path | None = None
    started: float = field(default_factory=time.monotonic)

    @property
    def frac(self) -> float:
        return min(self.received / self.total, 1.0) if self.total else 0.0


async def download(entry: MarketEntry, state: DownloadState,
                   cancel: Callable | None = None,
                   chunk: int = 1 << 16,
                   client_factory=None) -> DownloadState:
    """Stream a market file into the LM Studio folder. HF follows redirects
    to the CDN; we resume from an existing .part when the server agrees."""
    dest_dir = download_dest(entry)
    dest_dir.mkdir(parents=True, exist_ok=True)
    urls = [(entry.url, entry.file)]
    if entry.companion:
        urls.append((entry.companion_url or "", entry.companion))
    state.total = int(entry.size_gb * 1e9)
    state.dest = dest_dir
    factory = client_factory or (lambda: httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)))
    grand = 0
    try:
        async with factory() as c:
            for url, fname in urls:
                part = dest_dir / (fname + ".part")
                have = part.stat().st_size if part.exists() else 0
                headers = {"Range": f"bytes={have}-"} if have else {}
                async with c.stream("GET", url, headers=headers) as r:
                    if r.status_code not in (200, 206):
                        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    mode = "ab" if r.status_code == 206 else "wb"
                    if mode == "wb":
                        have = 0
                    total_here = int(r.headers.get("content-length") or 0) + have
                    with open(part, mode) as fh:
                        t0 = time.monotonic()
                        last_emit = t0
                        async for blk in r.aiter_bytes(chunk_size=chunk):
                            fh.write(blk)
                            have += len(blk)
                            grand += len(blk)
                            state.received = min(grand, state.total)
                            now = time.monotonic()
                            if now - last_emit >= 0.4:
                                done_bytes = have
                                secs = now - t0
                                state.mbps = done_bytes / secs / 1e6 if secs > 0 else 0.0
                                rem = max(total_here - have, 0)
                                state.eta_s = rem / (state.mbps * 1e6) if state.mbps > 0 else 0.0
                                last_emit = now
                            if cancel is not None and cancel():
                                state.error = "cancelled"
                                return state
                part.rename(dest_dir / fname)
        state.done = True
        state.received = state.total
    except Exception as exc:  # noqa: BLE001 — surfaced through state
        state.error = str(exc)[:200] or exc.__class__.__name__
    return state
