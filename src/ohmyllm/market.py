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
    short: str = ""  # one-line pitch for the detail pane header
    section: str = "llm"  # llm | stt — voice models land in ~/.ohmyllm/voice/
    zipball: bool = False  # fetch the whole repo archive (sample packs)
    kind: str = "gguf"  # gguf -> LM Studio folder; ninfer -> own runtime
    companion: str | None = None  # mmproj file needed for vision, same repo
    mtp: str | None = None  # MTP drafter file, same repo — auto-fetched beside the model
    alt: MarketEntry | None = None  # same model, other runtime format
    tags: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        if self.zipball:
            return f"https://huggingface.co/{self.repo}/archive/main.zip"
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.file}"

    @property
    def companion_url(self) -> str | None:
        if not self.companion:
            return None
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.companion}"

    @property
    def mtp_url(self) -> str | None:
        if not self.mtp:
            return None
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.mtp}"


# The curated shelf — what the author actually runs. Sizes are the real
# file sizes on the Hugging Face servers (verified 2026-10).
CATALOG: list[MarketEntry] = [
    MarketEntry(
        mid="qwen38-iq2xs",
        title="Qwen3.8-27B · IQ2_XS (GSQ-RCO)",
        repo="ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF",
        file="Qwen3.8-27B-GSQ-RCO-IQ2_XS.gguf",
        size_gb=8.4,
        short="ETH research quant — best 27B per gigabyte, vision",
        blurb="GSQ (Gumbel-Softmax) learns the scalar grid per tensor, RCO "
              "assigns quant types under a size budget. At the SAME 8.4 GB it "
              "beats Unsloth's dynamic quants by ~7.7% wikitext perplexity. "
              "Thinking model; vision-capable (mmproj included).",
        companion="mmproj-Qwen3.8-27B-BF16.gguf",
        tags=("thinking", "vision", "fits 12 GB"),
    ),
    MarketEntry(
        mid="qwen38-q6k",
        title="Qwen3.8-27B · Q6_K (Unsloth)",
        repo="unsloth/Qwen3.8-27B-GGUF",
        file="Qwen3.8-27B-UD-Q6_K.gguf",
        size_gb=22.0,
        short="near-original quality — the 24 GB+ card pick",
        blurb="Unsloth Dynamic 2.0: near-original quality, the safe choice "
              "when you have the VRAM. ~2.6x the size of IQ2_XS for a few "
              "points of perplexity — pick this for coding/agent work on "
              "24 GB+ cards (3090/4090/5090, Mac 32 GB+).",
        companion="mmproj-F16.gguf",
        tags=("thinking", "vision", "needs 24 GB"),
    ),
    MarketEntry(
        mid="sushi-9b",
        title="Qwen3.5-9B Sushi-Coder-RL · Q6_K",
        repo="bigatuna/Qwen3.5-9b-Sushi-Coder-RL-GGUF",
        file="Qwen3.5-9b-Sushi-Coder-RL.Q6_K.gguf",
        size_gb=7.4,
        short="RL coder that always thinks — fits 8 GB cards",
        blurb="RL-tuned coder that always thinks (forced reasoning). Sharp on "
              "refactors and tool calls, small enough for 8-12 GB cards and "
              "Macs. Vision companion included.",
        companion="Qwen3.5-9b-Sushi-Coder-RL.BF16-mmproj.gguf",
        tags=("forced thinking", "vision", "coder", "fits 8 GB"),
    ),
    MarketEntry(
        mid="spark-x26-4b",
        title="Spark-X2.6-4B · Q8_0",
        repo="XHToken/Spark-X2.6-4B-GGUF",
        file="Spark-X2.6-4B-Q8_0.gguf",
        size_gb=4.4,
        short="on-device agent 4B — thinking none/low/high, near-lossless Q8",
        blurb="SparkLLM's on-device flagship: best-in-class 4B agent/coder "
              "that matches ~9B models, Apache-2.0, en+zh. Q8_0 is the "
              "near-lossless quant at 4.4 GB — fits any 6 GB+ card with "
              "room for context. Controllable thinking budget: /think off "
              "answers straight, /think low thinks briefly, /think high "
              "thinks fully (medium/xhigh/max clamp to the model's high; "
              "/ultrathink rides it with a raised reply cap). Native tool "
              "calling — needs LM Studio 0.4+ or llama.cpp b10828+.",
        tags=("thinking levels", "agent", "tools", "fits 6 GB"),
    ),
    MarketEntry(
        mid="gemma4-26b",
        title="Gemma-4-26B-A4B QAT · Q4_0",
        repo="google/gemma-4-26B-A4B-it-qat-q4_0-gguf",
        file="gemma-4-26B_q4_0-it.gguf",
        size_gb=14.4,
        short="MoE 26B/4B-active, vision, official Google QAT",
        blurb="Mixture-of-Experts (26B total / 4B active) — fast like a 4B, "
              "smart like a 26B, vision built in. Official Google QAT Q4_0 "
              "in GGUF for LM Studio. If a NInfer runtime is detected on "
              "this machine, the marketplace automatically swaps to the "
              "WaveCut MTP-speculative NInfer build of the same weights "
              "instead (faster decode, same quant).",
        companion="gemma-4-26B-it-mmproj.gguf",
        alt=MarketEntry(
            mid="gemma4-26b-ninfer",
            title="Gemma-4-26B-A4B QAT · Q4_0 (NInfer)",
            repo="WaveCut/gemma-4-26B-A4B-it-QAT-Q4_0-MTP-NInfer-v3",
            file="gemma-4-26B-A4B-it-QAT-Q4_0-MTP-ninfer-v3.ninfer",
            size_gb=15.8,
            short="same weights, NInfer MTP build",
            blurb="NInfer runtime build with MTP speculative decoding.",
            kind="ninfer",
        ),
        tags=("vision", "MoE", "fast"),
    ),
    MarketEntry(
        mid="gemma4-12b",
        title="Gemma-4-12B QAT · Q4_K_XL (Unsloth)",
        repo="unsloth/gemma-4-12B-it-qat-GGUF",
        file="gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
        size_gb=6.7,
        short="QAT quality at 6.7 GB — MTP drafter auto-included",
        blurb="Unsloth QAT (quantization-aware trained) 12B in UD-Q4_K_XL: "
              "vision built in, thinking toggle, and the best part — the "
              "marketplace also fetches the matching MTP multi-token-prediction "
              "drafter (0.3 GB) right next to it. llama.cpp builds from "
              "2026-06-07+ auto-discover it: enable speculative decoding "
              "(LM Studio: model runtime tab; llama.cpp: --spec-type "
              "draft-mtp) for a big decode speedup with zero quality loss.",
        companion="mmproj-F16.gguf",
        mtp="mtp-gemma-4-12B-it.gguf",
        tags=("vision", "on-off thinking", "MTP drafter", "fits 8 GB"),
    ),
    MarketEntry(
        mid="gpt-oss-20b",
        title="GPT-OSS-20B · Q4_K_M",
        repo="unsloth/gpt-oss-20b-GGUF",
        file="gpt-oss-20b-Q4_K_M.gguf",
        size_gb=11.6,
        short="OpenAI's open reasoning MoE — honors /think levels",
        blurb="One of the few local models whose reasoning_effort levels "
              "actually change thinking length (low/medium/high). MoE 20B/"
              "3.6B-active — fast, sharp tool caller, fits 12-16 GB.",
        tags=("thinking levels", "tools", "MoE"),
    ),
    MarketEntry(
        mid="qwen3-coder-30b",
        title="Qwen3-Coder-30B-A3B · Q4_K_M",
        repo="unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF",
        file="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        size_gb=18.6,
        short="the agentic coder MoE — 24 GB+ cards",
        blurb="30B total / 3B active: long-context agentic coding that "
              "punches far above its active size. The model to run for "
              "OhMyLLM's tool loop on a 4090/Mac 32 GB.",
        tags=("coder", "agent", "MoE"),
    ),
    MarketEntry(
        mid="r1-14b",
        title="DeepSeek-R1-Distill-14B · Q4_K_M",
        repo="unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF",
        file="DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
        size_gb=9.0,
        short="pure reasoning model — always thinks",
        blurb="R1 distillation: extended chain-of-thought on tap in 9 GB. "
              "Pairs perfectly with /ultrathink for math and logic-heavy "
              "asks. No vision, no tools — a thinker.",
        tags=("forced thinking", "fits 12 GB"),
    ),
    MarketEntry(
        mid="phi-4",
        title="Phi-4 14B · Q4_K_M",
        repo="unsloth/phi-4-GGUF",
        file="phi-4-Q4_K_M.gguf",
        size_gb=8.9,
        short="Microsoft's compact heavyweight",
        blurb="Exceptionally strong for 14B on reasoning and instruction "
              "following. Solid all-rounder when you want quality in under "
              "9 GB without an RL-thinking tax on every reply.",
        tags=("instruct", "fits 12 GB"),
    ),
]


# ------------------------------------------------------------- voice shelf
# STT: SenseVoice (multilingual default: en/ko/zh/ja/yue), Parakeet (best
# English), Whisper multilingual ggmls (the "everything else" ~99-language
# family; pair with WhisperX/VAD for long files).
STT_CATALOG: list[MarketEntry] = [
    MarketEntry(
        mid="stt-sensevoice", section="stt",
        title="SenseVoice-Small · Q8 (FunAudioLLM)",
        repo="FunAudioLLM/SenseVoiceSmall-GGUF", file="sensevoice-small-q8.gguf",
        size_gb=0.254,
        short="multilingual default — English · Korean · Chinese · Japanese · Cantonese",
        blurb="Alibaba's SenseVoice in GGUF: 254 MB, auto language detection "
              "across en/ko/zh/ja/yue and more, very fast on CPU. Runtime: "
              "sensevoice.cpp (provides sensevoice-cli — OhMyLLM finds it on PATH).",
        tags=("stt", "multilingual", "ko", "zh", "en", "ja"),
    ),
    MarketEntry(
        mid="stt-parakeet", section="stt",
        title="Parakeet TDT 0.6B v3 · Q8 (NVIDIA)",
        repo="handy-computer/parakeet-tdt-0.6b-v3-gguf", file="parakeet-tdt-0.6b-v3-Q8_0.gguf",
        size_gb=0.74,
        short="the English pick — top EN accuracy, tiny footprint",
        blurb="NVIDIA Parakeet-TDT: best-in-class English WER at 0.6B params. "
              "English-only by design — pick SenseVoice or Whisper for other "
              "languages. Runtime: parakeet.cpp (parakeet-cli on PATH).",
        tags=("stt", "english"),
    ),
    MarketEntry(
        mid="stt-whisper-small", section="stt",
        title="Whisper small · multilingual (ggml)",
        repo="ggerganov/whisper.cpp", file="ggml-small.bin",
        size_gb=0.488,
        short="the everything-else option — ~99 languages incl. VAD via WhisperX",
        blurb="whisper.cpp multilingual build: near-universal language "
              "coverage. For long recordings add WhisperX (pip install "
              "whisperx) for voice-activity segmentation. Runtime: "
              "whisper-cli (whisper.cpp) — OhMyLLM finds it on PATH.",
        tags=("stt", "multilingual", "vad"),
    ),
    MarketEntry(
        mid="stt-whisper-turbo", section="stt",
        title="Whisper large-v3-turbo · Q5 (ggml)",
        repo="ggerganov/whisper.cpp", file="ggml-large-v3-turbo-q5_0.bin",
        size_gb=0.574,
        short="max multilingual accuracy, still fast",
        blurb="The accuracy pick when VRAM allows: large-v3-turbo quantized. "
              "Same ~99-language coverage + VAD-friendly.",
        tags=("stt", "multilingual", "accurate"),
    ),
]

CATALOG = CATALOG + STT_CATALOG


def resolve(entry: MarketEntry) -> MarketEntry:
    """Smart format pick: NInfer build only when a NInfer runtime exists."""
    if entry.alt is not None and entry.alt.kind == "ninfer":
        import shutil as _sh

        if _sh.which("ninfer"):
            return entry.alt
    return entry


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
    # compare in raw bytes: GGUF sizes are decimal GB, VRAM is binary —
    # 11.6 GB of weights on a 12 GiB card is tight, not impossible
    size_bytes = size_gb * 1e9
    label_gb = float(vram_gib(vram_bytes) or 0)
    if size_bytes <= vram_bytes * 0.80:
        return "fits", f"fits {label_gb:.0f} GB"
    if size_bytes <= vram_bytes * 0.95:
        return "tight", f"tight on {label_gb:.0f} GB"
    return "no", f"won't fit {label_gb:.0f} GB"


# ------------------------------------------------------------- lm studio dir
def lmstudio_models_dir() -> Path:
    env = os.environ.get("LMSTUDIO_MODEL_PATH") or os.environ.get("LMSTUDIO_MODELS_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".lmstudio" / "models"


def lmstudio_scan_roots() -> list[Path]:
    """Every place LM Studio may keep weights across versions/platforms."""
    roots = [lmstudio_models_dir(), Path.home() / ".cache" / "lm-studio" / "models"]
    return [r for r in dict.fromkeys(roots) if r.is_dir()]


def download_dest(entry: MarketEntry) -> Path:
    """Per-repo subfolder: two repos can ship identically named files
    (unsloth uses mmproj-F16.gguf for several models) — a flat folder would
    have them overwrite each other's vision projectors. Voice models live
    under ~/.ohmyllm/voice/ where the engine discovers them."""
    if entry.section == "stt":
        from .voice import voice_dir
        base = voice_dir() / entry.section
    else:
        sub = "ninfer" if entry.kind != "gguf" else "ohmyllm-market"
        base = lmstudio_models_dir() / sub
    slug = re.sub(r"[^A-Za-z0-9._-]+", "__", entry.repo.strip("/"))
    return base / slug


# --------------------------------------------------------------- vision scan
_MM_RE = re.compile(r"mmproj", re.IGNORECASE)


def scan_vision_models(root: Path | None = None) -> set[str]:
    """Model names (lowercase) that have an mmproj companion file on disk.

    LM Studio's API doesn't flag vision, but multimodal GGUFs always ship a
    sidecar `mmproj-*.gguf` / `*-mmproj.gguf` next to the weights — so the
    filesystem is the ground truth.
    """
    roots = [root] if root is not None else lmstudio_scan_roots()
    out: set[str] = set()
    try:
        for d in {p.parent for rt in roots for p in rt.rglob("*.gguf")}:
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


def scan_all_models(root: Path | None = None) -> set[str]:
    """Every non-mmproj GGUF name on disk — used to turn vision '?' into a
    definitive 'no' for models we actually saw without a sidecar."""
    roots = [root] if root is not None else lmstudio_scan_roots()
    out: set[str] = set()
    try:
        for rt in roots:
            for f in rt.rglob("*.gguf"):
                if not _MM_RE.search(f.name):
                    out.add(_name_from_file(f.stem))
    except OSError:
        pass
    return out


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
    try:
        entry = resolve(entry)
        dest_dir = download_dest(entry)
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — a bad dest must surface as state.error,
        state.error = f"{type(exc).__name__}: {exc}"  # never raise past the caller's
        state.done = True                        # thread that clears market_thread
        return state
    urls = [(entry.url, entry.file)]
    if entry.companion:
        urls.append((entry.companion_url or "", entry.companion))
    if entry.mtp:
        urls.append((entry.mtp_url or "", entry.mtp))
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
