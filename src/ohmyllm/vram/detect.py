"""GPU / memory detection: NVIDIA, AMD (rocm or sysfs), Apple Silicon, Windows."""

from __future__ import annotations

import glob
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class GPUInfo:
    vendor: str
    model: str
    total_bytes: int
    free_bytes: int | None = None
    source: str = "unknown"

    @property
    def total_gib(self) -> float:
        return self.total_bytes / 2**30


def _run(argv: list[str], timeout: float = 6.0) -> str | None:
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _which(name: str) -> str | None:
    from shutil import which

    return which(name) or (f"/opt/rocm/bin/{name}" if os.path.exists(f"/opt/rocm/bin/{name}") else None)


def _nvidia() -> list[GPUInfo]:
    exe = _which("nvidia-smi")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"])
    if not out:
        return []
    gpus: list[GPUInfo] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total, used = float(parts[1]) * 2**20, float(parts[2]) * 2**20
        except ValueError:
            continue
        gpus.append(GPUInfo("nvidia", parts[0], int(total), int(total - used), "nvidia-smi"))
    return gpus


def _rocm_smi() -> list[GPUInfo]:
    exe = _which("rocm-smi")
    if not exe:
        return []
    out = _run([exe, "--showmeminfo", "vram"])
    if not out:
        return []
    gpu = -1
    totals: dict[int, tuple[int, int]] = {}
    names: dict[int, str] = {}
    for line in out.splitlines():
        m = re.match(r"GPU\[(\d+)\]", line)
        if not m:
            continue
        gpu = int(m.group(1))
        if "Total Memory (B)" in line:
            v = _num_after(line)
            if v:
                t, u = totals.get(gpu, (v, 0))
                totals[gpu] = (v, u)
        elif "Used Memory (B)" in line:
            v = _num_after(line)
            if v:
                t, u = totals.get(gpu, (0, v))
                totals[gpu] = (t, v)
    gpus = []
    for i, (t, u) in sorted(totals.items()):
        if t <= 0:
            continue
        model = names.get(i) or _amdgpu_name(i) or f"AMD GPU {i}"
        gpus.append(GPUInfo("amd", model, t, max(t - u, 0), "rocm-smi"))
    return gpus


def _num_after(line: str) -> int | None:
    m = re.search(r":\s*([\d_]+)\s*$", line)
    return int(m.group(1)) if m else None


def _amdgpu_name(idx: int) -> str | None:
    p = f"/sys/class/drm/card{idx}/device/uevent"
    try:
        txt = open(p, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"PCI_SLOT_NAME=(\S+)", txt)
    return f"AMDGPU {m.group(1)}" if m else None


def _sysfs_amd() -> list[GPUInfo]:
    gpus = []
    for total_f in sorted(glob.glob("/sys/class/drm/card*/device/mem_info_vram_total")):
        try:
            total = int(open(total_f, encoding="utf-8").read().strip())
            used_p = total_f.replace("mem_info_vram_total", "mem_info_vram_used")
            used = int(open(used_p, encoding="utf-8").read().strip()) if os.path.exists(used_p) else 0
        except (OSError, ValueError):
            continue
        if total <= 0:
            continue
        gpus.append(GPUInfo("amd", _amdgpu_name_from_path(total_f), total, max(total - used, 0), "sysfs"))
    return gpus


def _amdgpu_name_from_path(p: str) -> str:
    m = re.search(r"/(card\d+)/", p)
    return f"AMDGPU {m.group(1)}" if m else "AMD GPU"


def _apple() -> list[GPUInfo]:
    out = _run(["sysctl", "-n", "hw.memsize"])
    if not out:
        return []
    try:
        total = int(out.strip())
    except ValueError:
        return []
    name = "Apple Silicon (unified memory)"
    prof = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=8.0)
    if prof:
        m = re.search(r'"sppci_model"\s*:\s*"([^"]+)"', prof)
        if m:
            name = f"Apple {m.group(1)} (unified)"
    used = _run(["memory_pressure"], timeout=4.0) or ""
    free = None
    m = re.search(r"free pages.*?:\s*(\d+)", used)
    if m:
        try:
            free = int(m.group(1)) * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError):
            free = None
    return [GPUInfo("apple", name, total, free, "sysctl")]


def _windows() -> list[GPUInfo]:
    if platform.system() != "Windows":
        return []
    script = (
        "Get-CimInstance -ClassName Win32_VideoController | "
        "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
    )
    out = _run(["powershell", "-NoProfile", "-Command", script], timeout=12.0)
    if not out:
        return []
    import json

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    gpus = []
    for it in items:
        if not isinstance(it, dict) or not it.get("Name"):
            continue
        total = int(it.get("AdapterRAM") or 0)
        if total <= 0:
            continue
        gpus.append(GPUInfo("windows", str(it["Name"]), total, None, "powershell"))
    return gpus


def _detect_gpus_uncached() -> list[GPUInfo]:
    system = platform.system()
    if system == "Darwin":
        return _apple()
    if system == "Windows":
        # nvidia-smi works on Windows and reports true VRAM; the WMI
        # AdapterRAM field is a uint32 capped at 4 GB.
        return _nvidia() or _windows()
    gpus = _nvidia()
    if gpus:
        return gpus
    gpus = _rocm_smi()
    return gpus or _sysfs_amd()


# GPU topology barely changes, but the probes are expensive (powershell
# cold start on Windows can take seconds) and callers poll every few
# seconds. A short TTL keeps the status bar honest without subprocess
# churn. Callers that must re-probe now: invalidate_gpu_cache().
_GPU_TTL = 10.0
_gpu_cache: tuple[float, list[GPUInfo]] | None = None
_gpu_lock = threading.Lock()


def invalidate_gpu_cache() -> None:
    global _gpu_cache
    with _gpu_lock:
        _gpu_cache = None


def detect_gpus() -> list[GPUInfo]:
    global _gpu_cache
    with _gpu_lock:
        if _gpu_cache is not None and time.monotonic() - _gpu_cache[0] < _GPU_TTL:
            return _gpu_cache[1]
    gpus = _detect_gpus_uncached()
    with _gpu_lock:
        _gpu_cache = (time.monotonic(), gpus)
    return gpus


def detect_gpus_cached() -> list[GPUInfo]:
    """Snapshot of the last probe — never spawns subprocesses.

    The models screen renders fit estimates from this; freshness comes
    from the app-start warm and any other detect_gpus() caller (status
    bar, bridge heartbeat). A cold cache simply means "fit unknown",
    not a stalled table."""
    with _gpu_lock:
        if _gpu_cache is not None and time.monotonic() - _gpu_cache[0] < _GPU_TTL:
            return _gpu_cache[1]
    return []


def total_vram_bytes() -> int:
    return sum(g.total_bytes for g in detect_gpus())


def available_vram_bytes() -> int | None:
    gpus = detect_gpus()
    if not gpus:
        return None
    if all(g.free_bytes is None for g in gpus):
        return None
    return sum(g.free_bytes or g.total_bytes for g in gpus)


def cpu_ram_bytes() -> int | None:
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MS()
            stat.dwLength = ctypes.sizeof(stat)
            windll = getattr(ctypes, "windll", None)
            if windll is None:
                return None
            windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys)
        except Exception:  # noqa: BLE001
            return None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, KeyError):
        return None


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def gpu_summary() -> str:
    gpus = detect_gpus()
    if not gpus:
        return "no GPU detected (CPU/RAM only)"
    return ", ".join(f"{g.model} {human_bytes(g.total_bytes)}" for g in gpus)


__all__ = [
    "GPUInfo",
    "available_vram_bytes",
    "cpu_ram_bytes",
    "detect_gpus",
    "gpu_summary",
    "human_bytes",
    "total_vram_bytes",
]
