"""Hardware detection (3.0): measure what is actually free before choosing or downloading a model.

* CPU: name, physical/logical cores.
* RAM: total and *available* (not the advertised size).
* NVIDIA GPU: name, total/free VRAM, utilisation and driver, through ``nvidia-smi`` (installed with the NVIDIA driver).
  Integrated graphics (e.g. AMD Radeon 780M) share system RAM and are not used for model inference here.
* Disk: free space on the drive that holds Ollama's models (``OLLAMA_MODELS`` or ``~/.ollama/models``).

Everything degrades gracefully: a missing tool or permission gives ``None`` for that value, never an exception.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

GB = 1024 ** 3


@dataclass
class GPU:
    name: str
    vram_total_gb: float
    vram_free_gb: float
    utilisation_pct: Optional[float] = None
    driver: str = ""


@dataclass
class Hardware:
    cpu: str = ""
    cores_physical: Optional[int] = None
    cores_logical: Optional[int] = None
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    gpus: list[GPU] = field(default_factory=list)
    disk_free_gb: Optional[float] = None
    models_dir: str = ""
    os: str = ""

    @property
    def best_gpu(self) -> Optional[GPU]:
        return max(self.gpus, key=lambda g: g.vram_free_gb, default=None)

    @property
    def vram_free_gb(self) -> float:
        g = self.best_gpu
        return g.vram_free_gb if g else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vram_free_gb"] = self.vram_free_gb
        return d

    def summary(self) -> str:
        g = self.best_gpu
        gpu = (f"{g.name} with {g.vram_free_gb:.1f} of {g.vram_total_gb:.1f} GB video memory free" if g
               else "no NVIDIA GPU detected (models will run on the CPU)")
        disk = f", {self.disk_free_gb:.0f} GB free disk" if self.disk_free_gb is not None else ""
        cores = f" ({self.cores_physical} cores)" if self.cores_physical else ""
        return (f"{self.cpu or 'CPU'}{cores}, {self.ram_available_gb:.1f} of {self.ram_total_gb:.1f} GB RAM available, "
                f"{gpu}{disk}.")


Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=True, **kw).stdout


def _nvidia_smi() -> Optional[str]:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for p in (r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if Path(p).is_file():
            return p
    return None


def parse_nvidia_smi(text: str) -> list[GPU]:
    """Parse ``--query-gpu=name,memory.total,memory.free,utilization.gpu,driver_version --format=csv,noheader,nounits``."""
    gpus = []
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total, free = float(parts[1]) / 1024, float(parts[2]) / 1024  # MiB -> GiB
        except ValueError:
            continue
        util = None
        if len(parts) > 3:
            try:
                util = float(parts[3])
            except ValueError:
                util = None
        gpus.append(GPU(parts[0], round(total, 2), round(free, 2), util, parts[4] if len(parts) > 4 else ""))
    return gpus


def models_dir() -> Path:
    env = os.getenv("OLLAMA_MODELS")
    return Path(env) if env else Path.home() / ".ollama" / "models"


def _cpu_name() -> str:
    name = platform.processor() or ""
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
        except Exception:
            pass
    elif Path("/proc/cpuinfo").is_file():
        for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines():
            if line.lower().startswith("model name"):
                name = line.split(":", 1)[1]
                break
    return " ".join(name.split())


def detect(run: Runner = _run) -> Hardware:
    hw = Hardware(cpu=_cpu_name(), os=f"{platform.system()} {platform.release()}")
    try:
        import psutil

        vm = psutil.virtual_memory()
        hw.ram_total_gb = round(vm.total / GB, 1)
        hw.ram_available_gb = round(vm.available / GB, 1)
        hw.cores_physical = psutil.cpu_count(logical=False)
        hw.cores_logical = psutil.cpu_count(logical=True)
    except Exception:
        hw.cores_logical = os.cpu_count()
    smi = _nvidia_smi() if run is _run else "nvidia-smi"
    if smi:
        try:
            out = run([smi, "--query-gpu=name,memory.total,memory.free,utilization.gpu,driver_version",
                       "--format=csv,noheader,nounits"])
            hw.gpus = parse_nvidia_smi(out)
        except Exception:
            hw.gpus = []
    md = models_dir()
    hw.models_dir = str(md)
    probe = md if md.exists() else Path.home()
    try:
        hw.disk_free_gb = round(shutil.disk_usage(str(probe)).free / GB, 1)
    except OSError:
        hw.disk_free_gb = None
    return hw
