"""VRAM detection and estimation."""

from .detect import (
    GPUInfo,
    available_vram_bytes,
    cpu_ram_bytes,
    detect_gpus,
    gpu_summary,
    human_bytes,
    total_vram_bytes,
)
from .estimator import (
    QUANT_BITS,
    VramEstimate,
    bits_per_weight,
    estimate_vram,
    kv_bytes_per_token,
    layers_for_params,
    model_size_and_quant_from_name,
)

__all__ = [
    "QUANT_BITS",
    "GPUInfo",
    "VramEstimate",
    "available_vram_bytes",
    "bits_per_weight",
    "cpu_ram_bytes",
    "detect_gpus",
    "estimate_vram",
    "gpu_summary",
    "human_bytes",
    "kv_bytes_per_token",
    "layers_for_params",
    "model_size_and_quant_from_name",
    "total_vram_bytes",
]
