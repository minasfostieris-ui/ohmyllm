"""Heuristic VRAM estimation for quantized local models.

Heuristics, not measurements: the goal is a trustworthy ballpark to warn
about OOM before loading. Notes are surfaced in the UI detail panel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: nominal bits/weight for common llama.cpp quant tags
QUANT_BITS: dict[str, float] = {
    "q2_k": 2.6, "q3_k_s": 3.1, "q3_k_m": 3.5, "q3_k_l": 3.9,
    "q4_0": 4.5, "q4_1": 4.5, "q4_k_s": 4.5, "q4_k_m": 4.8, "q5_0": 5.5,
    "q5_1": 5.5, "q5_k_s": 5.6, "q5_k_m": 5.8, "q6_k": 6.6, "q8_0": 8.5,
    "f16": 16.0, "f32": 32.0, "bf16": 16.0, "fp16": 16.0, "fp32": 32.0,
}

_KV_DTYPE_BYTES = 2  # fp16 K/V cache
_DEFAULT_KV_DIM = 1024  # typical GQA kv hidden dim for 7–14B models
_RUNTIME_OVERHEAD = 450 * 2**20  # CUDA/ROCm/MoltenVK context + buffers
_LAYER_TABLE: list[tuple[float, int]] = [
    (0.5, 21), (1.5, 24), (3.0, 26), (4.0, 32), (7.0, 32), (8.0, 36),
    (12.0, 40), (13.0, 40), (24.0, 48), (27.0, 48), (34.0, 60), (70.0, 80),
    (110.0, 88), (123.0, 96), (200.0, 100), (405.0, 104),
]


@dataclass
class VramEstimate:
    weights_bytes: int = 0
    offloaded_weights_bytes: int = 0
    kv_bytes_at_ctx: int = 0
    overhead_bytes: int = 0
    min_bytes: int = 0
    comfortable_bytes: int = 0
    gpu_total_bytes: int | None = None
    gpu_free_bytes: int | None = None
    fits_min: bool | None = None
    fits_comfortable: bool | None = None
    offload_ratio: float = 1.0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.fits_comfortable:
            return "comfortable"
        if self.fits_min:
            return "tight"
        if self.fits_min is False:
            return "too_large"
        return "unknown"


def bits_per_weight(quant: str | None, size_bytes: int | None = None, params_b: float | None = None) -> float | None:
    """Known tag → nominal bits; else derive from size/params when both known."""
    if quant:
        key = quant.lower().replace("-", "_").strip()
        key = re.sub(r"_?(rope|iq|0|1)$", "", key) if key not in QUANT_BITS else key
        for k, v in QUANT_BITS.items():
            if key.startswith(k) or k in key:
                return v
    if size_bytes and params_b and params_b > 0:
        return (size_bytes * 8) / (params_b * 1e9)
    return None


def layers_for_params(params_b: float) -> int:
    """Log-linear interpolation over a measured param→layer table."""
    if params_b <= 0:
        return 32
    import math

    (_p0, l0), (_p1, _l1) = _LAYER_TABLE[0], _LAYER_TABLE[1]
    for (pa, la), (pb, lb) in zip(_LAYER_TABLE, _LAYER_TABLE[1:], strict=False):
        if pa <= params_b <= pb:
            t = (math.log(params_b) - math.log(pa)) / (math.log(pb) - math.log(pa))
            return round(la + t * (lb - la))
    if params_b < _LAYER_TABLE[0][0]:
        return l0
    if params_b > _LAYER_TABLE[-1][0]:
        return _LAYER_TABLE[-1][1]
    return 40  # unreachable; safe default


def kv_bytes_per_token(layers: int, kv_dim: int = _DEFAULT_KV_DIM) -> int:
    return 2 * layers * kv_dim * _KV_DTYPE_BYTES


def estimate_vram(
    size_bytes: int,
    *,
    num_ctx: int = 4096,
    quant: str | None = None,
    params_b: float | None = None,
    num_gpu_layers: int = -1,
    total_layers: int | None = None,
    batch: int = 512,
    gpu_total_bytes: int | None = None,
    gpu_free_bytes: int | None = None,
) -> VramEstimate:
    est = VramEstimate(weights_bytes=int(size_bytes))
    est.notes.append("Heuristic estimate — actual usage depends on the runtime, KV dtype and heads.")

    layers = total_layers or (layers_for_params(params_b) if params_b else 32)
    ratio = 1.0
    if num_gpu_layers == 0:
        ratio = 0.0
    elif num_gpu_layers > 0 and total_layers:
        ratio = max(0.0, min(1.0, num_gpu_layers / total_layers))
    elif num_gpu_layers > 0 and not total_layers:
        est.notes.append(f"num_gpu={num_gpu_layers} but layer count unknown; assuming full GPU offload.")
    est.offload_ratio = ratio

    est.offloaded_weights_bytes = int(size_bytes * ratio)
    est.kv_bytes_at_ctx = kv_bytes_per_token(layers) * num_ctx
    batch_factor = 1.0 + max(0, batch - 1) / 512 * 0.05
    est.overhead_bytes = _RUNTIME_OVERHEAD if ratio > 0 else 0

    est.min_bytes = int(est.offloaded_weights_bytes + est.kv_bytes_at_ctx * 0.35 * batch_factor + est.overhead_bytes * 0.6)
    est.comfortable_bytes = int(est.offloaded_weights_bytes * 1.08 + est.kv_bytes_at_ctx * batch_factor + est.overhead_bytes * 1.25)

    est.gpu_total_bytes = gpu_total_bytes
    est.gpu_free_bytes = gpu_free_bytes
    budget = gpu_free_bytes or gpu_total_bytes
    if budget:
        est.fits_min = est.min_bytes <= budget
        est.fits_comfortable = est.comfortable_bytes <= budget
        if not est.fits_min:
            est.warnings.append(
                "Likely too large for available GPU memory — lower num_ctx, use a smaller "
                "quant, or reduce GPU offload (the rest spills to CPU/RAM and slows down)."
            )
        elif not est.fits_comfortable:
            est.warnings.append("Tight fit: short context and no other GPU users recommended.")
    else:
        est.notes.append("GPU memory not detected; verify manually before loading large models.")
    if ratio == 0.0:
        est.warnings.append("num_gpu=0 → CPU/RAM inference; expect slow tokens/s for agent loops.")
    bits = bits_per_weight(quant, size_bytes, params_b)
    if bits is not None:
        est.notes.append(f"~{bits:.1f} bits/weight ({quant or 'derived from size'}).")
    if params_b:
        est.notes.append(f"~{layers} layers assumed for {params_b:g}B params (KV cache scales with layers × ctx).")
    return est


def model_size_and_quant_from_name(name: str) -> tuple[str | None, float | None]:
    """Parse 'qwen2.5:7b-instruct-q4_K_M' style tags → (quant, params_b)."""
    quant = None
    params = None
    m = re.search(r"(q\d(?:\.\d)?_?[kisl]_?[misl]|fp16|f16|f32|q8_0)", name, re.IGNORECASE)
    if m:
        quant = m.group(1)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?:[^a-zA-Z]|$)", name)
    if m:
        params = float(m.group(1))
    return quant, params


__all__ = [
    "QUANT_BITS",
    "VramEstimate",
    "bits_per_weight",
    "estimate_vram",
    "kv_bytes_per_token",
    "layers_for_params",
    "model_size_and_quant_from_name",
]
