import ohmyllm.vram.detect as D
from ohmyllm.vram import (
    bits_per_weight,
    estimate_vram,
    layers_for_params,
    model_size_and_quant_from_name,
)
from ohmyllm.vram.detect import _nvidia, _rocm_smi

ROCM_FIXTURE = """
============================ ROCm System Management Interface ============================
================================== Memory Usage (Bytes) ==================================
GPU[0]		: VRAM Total Memory (B): 12868124672
GPU[0]		: VRAM Total Used Memory (B): 1382907904
==========================================================================================
================================== End of ROCm SMI Log ===================================
"""

NV_FIXTURE = """NVIDIA GeForce RTX 3080, 10240, 2048
NVIDIA GeForce RTX 3080, 10240, 512
"""


def test_rocm_parse(monkeypatch):
    monkeypatch.setattr(D, "_which", lambda n: "/opt/rocm/bin/rocm-smi")
    monkeypatch.setattr(D, "_run", lambda argv, timeout=6.0: ROCM_FIXTURE)
    gpus = _rocm_smi()
    assert len(gpus) == 1
    assert gpus[0].total_bytes == 12868124672
    assert gpus[0].free_bytes == 12868124672 - 1382907904


def test_nvidia_parse(monkeypatch):
    monkeypatch.setattr(D, "_which", lambda n: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(D, "_run", lambda argv, timeout=6.0: NV_FIXTURE)
    gpus = _nvidia()
    assert len(gpus) == 2 and gpus[0].total_bytes == 10240 * 2**20


def test_human_bytes():
    from ohmyllm.vram.detect import human_bytes

    assert human_bytes(12868124672).endswith("GiB")
    assert human_bytes(None) == "?"


def test_bits_per_weight():
    assert bits_per_weight("Q4_K_M") == 4.8
    assert abs(bits_per_weight(None, size_bytes=4 * 10**9, params_b=7.0) - (4 * 8 / 7)) < 0.1
    assert bits_per_weight("fp16") == 16.0


def test_layers_table():
    assert layers_for_params(7) == 32
    assert 36 <= layers_for_params(8) <= 40
    assert layers_for_params(70) == 80
    assert layers_for_params(0.5) == 21


def test_estimate_fits_and_warns():
    e = estimate_vram(4_400 * 2**20, num_ctx=8192, params_b=7.6, gpu_total_bytes=12 * 2**30)
    assert e.weights_bytes == 4_400 * 2**20
    assert e.min_bytes < e.comfortable_bytes
    assert e.fits_comfortable is True or e.status in ("comfortable", "tight")
    small = estimate_vram(40 * 2**30, num_ctx=8192, params_b=70, gpu_total_bytes=8 * 2**30)
    assert small.status == "too_large"
    assert any("too large" in w for w in small.warnings)
    cpu = estimate_vram(4 * 2**30, num_gpu_layers=0)
    assert cpu.offload_ratio == 0.0 and cpu.overhead_bytes == 0
    assert any("CPU/RAM" in w for w in cpu.warnings)


def test_bigger_ctx_more_kv():
    a = estimate_vram(4_000 * 2**20, num_ctx=4096, params_b=7)
    b = estimate_vram(4_000 * 2**20, num_ctx=32768, params_b=7)
    kv_delta = b.kv_bytes_at_ctx - a.kv_bytes_at_ctx
    assert b.comfortable_bytes - a.comfortable_bytes >= kv_delta
    assert b.min_bytes > a.min_bytes


def test_name_parsing():
    q, p = model_size_and_quant_from_name("qwen2.5:7b-instruct-q4_K_M")
    assert p == 7.0 and q and q.lower().startswith("q4")
